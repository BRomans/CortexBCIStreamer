import threading
import time
import numpy as np
import logging
import pyqtgraph as pg
import os
import yaml
from PyQt5 import QtWidgets, QtCore
from application.classifier import Classifier
from application.lsl.lsl_stream import LSLStreamThread, start_lsl_eeg_stream, start_lsl_power_bands_stream, \
    start_lsl_prediction_stream, start_lsl_quality_stream, push_lsl_raw_eeg, push_lsl_band_powers, push_lsl_prediction, \
    push_lsl_quality
from utils.layouts import layouts
from pyqtgraph import ScatterPlotItem, mkBrush
from brainflow.board_shim import BoardShim
from brainflow.data_filter import DataFilter, FilterTypes, DetrendOperations
from concurrent.futures import ThreadPoolExecutor
from processing.preprocessing import extract_band_powers
from processing.proc_helper import freq_bands

# 16 Color ascii codes for the 16 EEG channels
colors = ["blue", "green", "yellow", "purple", "orange", "pink", "brown", "gray",
          "cyan", "magenta", "lime", "teal", "lavender", "turquoise", "maroon", "olive"]


def write_header(file, board_id):
    for column in layouts[board_id]["header"]:
        file.write(str(column) + '\t')
    file.write('\n')


class Streamer:

    def __init__(self, board, params, window_size=1, config_file='config.yaml'):
        # Load configuration from file
        with open(config_file, 'r') as file:
            config = yaml.safe_load(file)

        self.window_size = window_size
        # Apply configuration
        self.plot = config.get('plot', True)
        self.save_data = config.get('save_data', True)
        self.model = config.get('model', 'LDA')
        self.nclasses = config.get('nclasses', 4)
        self.on_time = config.get('on_time', 250)
        self.quality_thresholds = config.get('quality_thresholds', [(-100, -50, 'yellow', 0.5), (-50, 50, 'green', 1.0),
                                                                    (50, 100, 'yellow', 0.5)])
        self.update_speed_ms = config.get('update_speed_ms', 1000 * self.window_size)
        self.update_plot_speed_ms = config.get('update_plot_speed_ms', 1000 / self.window_size)

        time.sleep(self.window_size)  # Wait for the board to be ready
        self.is_streaming = True
        self.prediction_mode = False
        self.first_prediction = True
        self.params = params
        self.initial_ts = time.time()
        logging.info("Searching for devices...")
        self.board = board
        self.board_id = self.board.get_board_id()
        self.eeg_channels = BoardShim.get_eeg_channels(self.board_id)
        self.sampling_rate = BoardShim.get_sampling_rate(self.board_id)
        self.chunk_counter = 0
        self.num_points = self.window_size * self.sampling_rate
        self.filtered_eeg = np.zeros((len(self.eeg_channels) + 1, self.num_points))
        logging.info(f"Connected to {self.board.get_device_name(self.board.get_board_id())}")

        # Initialize the classifier in a new thread
        self.classifier = None
        self.executor = ThreadPoolExecutor(max_workers=5)
        if self.model is not None:
            self.over_sample = False
            self.classifier_thread = threading.Thread(target=self.init_classifier)
            self.classifier_thread.start()

        self.app = QtWidgets.QApplication([])

        # Calculate time interval for prediction
        self.off_time = (self.on_time * (self.nclasses - 1))
        logging.debug(f"Off time: {self.off_time} ms")
        self.prediction_interval = int(
            self.on_time + self.off_time) * 2  # we take double the time, so we can loop on it
        logging.debug(f"Prediction interval: {self.prediction_interval} ms")
        # calculate how many datapoints based on the sampling rate
        self.prediction_datapoints = int(self.prediction_interval * self.sampling_rate / 1000)
        logging.debug(f"Prediction interval in datapoints: {self.prediction_datapoints}")

        logging.info("Looking for an LSL stream...")
        # Connect to the LSL stream threads
        self.prediction_timer = QtCore.QTimer()
        self.lsl_thread = LSLStreamThread()
        self.lsl_thread.new_sample.connect(self.write_trigger)
        self.lsl_thread.set_train_start.connect(self.set_train_start)
        self.lsl_thread.start_train.connect(self.train_classifier)
        self.lsl_thread.start_predicting.connect(self.set_prediction_mode)
        self.lsl_thread.stop_predicting.connect(self.set_prediction_mode)
        self.lsl_thread.start()

        self.win = pg.GraphicsLayoutWidget(title='Cortex Streamer', size=(1200, 800))
        self.win.setWindowTitle('Cortex Streamer')
        self.win.show()
        panel = self.create_buttons()
        plot = self.init_plot()

        # Create a layout for the main window
        self.main_layout = QtWidgets.QGridLayout()
        self.main_layout.addWidget(plot, 0, 0)
        self.main_layout.addWidget(panel, 1, 0)

        # Set the main layout for the window
        self.win.setLayout(self.main_layout)

        # Start the PyQt event loop to fetch the raw data
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_data_buffer)
        self.timer.start(self.update_speed_ms)

        self.plot_timer = QtCore.QTimer()
        self.plot_timer.timeout.connect(self.update_plot)
        self.plot_timer.start(self.update_plot_speed_ms)

        # Initialize LSL streams
        self.eeg_outlet = start_lsl_eeg_stream(channels=layouts[self.board_id]["channels"], fs=self.sampling_rate,
                                               source_id=self.board.get_device_name(self.board_id))
        self.prediction_outlet = start_lsl_prediction_stream(fs=self.sampling_rate,
                                                             source_id=self.board.get_device_name(self.board_id))
        self.band_powers_outlet = start_lsl_power_bands_stream(channels=layouts[self.board_id]["channels"],
                                                               fs=self.sampling_rate,
                                                               source_id=self.board.get_device_name(self.board_id))
        self.quality_outlet = start_lsl_quality_stream(channels=layouts[self.board_id]["channels"],
                                                       fs=self.sampling_rate,
                                                       source_id=self.board.get_device_name(self.board_id))

        self.app.exec_()

    def create_buttons(self):
        """Create buttons to interact with the streamer"""

        # Button to write trigger and input box to specify the trigger value
        self.input_box = QtWidgets.QLineEdit()
        self.input_box.setFixedWidth(100)  # Set a fixed width for the input box
        self.input_box.setPlaceholderText('Trigger value')
        self.input_box.setText('1')

        self.trigger_button = QtWidgets.QPushButton('Send Trigger')
        self.trigger_button.setFixedWidth(100)  # Set a fixed width for the button
        self.trigger_button.clicked.connect(lambda: self.write_trigger(int(self.input_box.text())))

        # Start / Stop buttons
        self.start_button = QtWidgets.QPushButton('Stop')
        self.start_button.setFixedWidth(100)
        self.start_button.clicked.connect(lambda: self.toggle_stream())

        # Buttons to plot ROC curve and confusion matrix
        self.roc_button = QtWidgets.QPushButton('Plot ROC')
        self.roc_button.setFixedWidth(100)
        self.roc_button.clicked.connect(lambda: self.classifier.plot_roc_curve())

        self.confusion_button = QtWidgets.QPushButton('Plot CM')
        self.confusion_button.setFixedWidth(100)
        self.confusion_button.clicked.connect(lambda: self.classifier.plot_confusion_matrix())

        # Save data checkbox
        self.save_data_checkbox = QtWidgets.QCheckBox('Save data to file')
        self.save_data_checkbox.setStyleSheet('color: white')
        self.save_data_checkbox.setChecked(self.save_data)

        # Input box to configure Bandpass filter with checkbox to enable/disable it
        self.bandpass_checkbox = QtWidgets.QCheckBox('Bandpass filter frequencies (Hz)')
        self.bandpass_checkbox.setStyleSheet('color: white')
        self.bandpass_box_low = QtWidgets.QLineEdit()
        self.bandpass_box_low.setPlaceholderText('0')
        self.bandpass_box_low.setText('1')
        self.bandpass_box_low.setMaximumWidth(30)
        self.bandpass_box_high = QtWidgets.QLineEdit()
        self.bandpass_box_high.setPlaceholderText('0')
        self.bandpass_box_high.setText('40')
        self.bandpass_box_high.setMaximumWidth(30)

        # Input box to configure Notch filter with checkbox to enable/disable it and white label
        self.notch_checkbox = QtWidgets.QCheckBox('Notch filter frequencies (Hz)')
        self.notch_checkbox.setStyleSheet('color: white')
        self.notch_box = QtWidgets.QLineEdit()
        self.notch_box.setMaximumWidth(60)  # Set a fixed width for the input box
        self.notch_box.setPlaceholderText('0, 0')
        self.notch_box.setText('50, 60')

        self.lsl_chunk_checkbox = QtWidgets.QCheckBox('Chunk data')
        self.lsl_chunk_checkbox.setStyleSheet('color: white')
        self.lsl_chunk_checkbox.setChecked(True)

        # Create a layout for buttons
        start_save_layout = QtWidgets.QHBoxLayout()
        start_save_layout.addWidget(self.save_data_checkbox)
        start_save_layout.addWidget(self.start_button)

        # Create a layout for the bandpass filter
        bandpass_layout = QtWidgets.QHBoxLayout()
        bandpass_layout.addWidget(self.bandpass_checkbox)
        bandpass_layout.addWidget(self.bandpass_box_low)
        bandpass_layout.addWidget(self.bandpass_box_high)


        # Create a layout for the notch filter
        notch_layout = QtWidgets.QHBoxLayout()
        notch_layout.addWidget(self.notch_checkbox)
        notch_layout.addWidget(self.notch_box)

        # Create a layout for LSL options
        lsl_layout_label = QtWidgets.QLabel("LSL Options")
        lsl_layout_label.setStyleSheet("color: white; font-size: 20px;")
        lsl_layout = QtWidgets.QVBoxLayout()
        lsl_layout.addWidget(lsl_layout_label)
        lsl_layout.addWidget(self.lsl_chunk_checkbox)

        # Create a vertical layout to contain the notch filter and the button layout
        left_side_label = QtWidgets.QLabel("Filters")
        left_side_label.setStyleSheet("color: white; font-size: 20px;")
        left_side_layout = QtWidgets.QVBoxLayout()
        left_side_layout.addWidget(left_side_label)
        left_side_layout.addLayout(bandpass_layout)
        left_side_layout.addLayout(notch_layout)
        left_side_layout.addLayout(start_save_layout)

        # Create a center layout for trigger button
        center_label = QtWidgets.QLabel("Markers")
        center_label.setStyleSheet("color: white; size: 20px;")
        center_layout = QtWidgets.QVBoxLayout()
        center_layout.addWidget(center_label)
        center_layout.addWidget(self.input_box)
        center_layout.addWidget(self.trigger_button)

        # Create a layout for classifier plots
        right_side_label = QtWidgets.QLabel("Classifier")
        right_side_label.setStyleSheet("color: white; font-size: 20px;")
        right_side_layout = QtWidgets.QVBoxLayout()
        right_side_layout.addWidget(right_side_label)
        right_side_layout.addWidget(self.roc_button)
        right_side_layout.addWidget(self.confusion_button)

        # Horizontal layout to contain the classifier buttons
        horizontal_container = QtWidgets.QHBoxLayout()
        horizontal_container.addLayout(lsl_layout)
        horizontal_container.addLayout(left_side_layout)
        horizontal_container.addLayout(center_layout)
        horizontal_container.addLayout(right_side_layout)

        # Create a widget to contain the layout
        button_widget = QtWidgets.QWidget()
        button_widget.setLayout(horizontal_container)

        return button_widget


    def set_prediction_mode(self):
        """Set the BCI running status"""
        self.prediction_mode = not self.prediction_mode
        self.classifier.set_prediction_mode(self.prediction_mode)

    def update_data_buffer(self):
        """ Update the plot with new data"""
        if self.is_streaming:
            if self.window_size == 0:
                raise ValueError("Window size cannot be zero")
            data = self.board.get_current_board_data(num_samples=self.num_points)
            self.filter_data_buffer(data)
            start_eeg = layouts[self.board_id]["eeg_start"]
            end_eeg = layouts[self.board_id]["eeg_end"]
            eeg = data[start_eeg:end_eeg]
            self.chunk_counter += 1

            for count, channel in enumerate(self.eeg_channels):
                ch_data = eeg[count]
                self.filtered_eeg[count] = ch_data

            trigger = data[-1]
            ts_channel = self.board.get_timestamp_channel(self.board_id)
            ts = data[ts_channel]
            self.filtered_eeg[-1] = trigger
            band_powers = extract_band_powers(data=self.filtered_eeg[0:len(self.eeg_channels)], fs=self.sampling_rate,
                                              bands=freq_bands, ch_names=self.eeg_channels)
            self.app.processEvents()
            push_lsl_raw_eeg(self.eeg_outlet, self.filtered_eeg, start_eeg, end_eeg, self.chunk_counter, ts,
                             self.lsl_chunk_checkbox.isChecked())
            push_lsl_band_powers(self.band_powers_outlet, band_powers.to_numpy(), ts)

    def init_plot(self):
        """Initialize the timeseries plot for the EEG channels and trigger channel."""


        # Initialize a single plot for all EEG channels including the trigger
        self.eeg_plot = pg.PlotWidget()  # Use PlotWidget to create a plot that can be added to a layout

        # Configure the plot settings
        self.eeg_plot.showAxis('left', False)  # Hide the Y-axis labels
        self.eeg_plot.setMenuEnabled('left', True)
        self.eeg_plot.showAxis('bottom', True)
        self.eeg_plot.setMenuEnabled('bottom', True)
        #self.eeg_plot.setMinimumWidth(800)  # Set a large minimum width
        self.eeg_plot.setLabel('bottom', text='Time (s)')
        self.eeg_plot.getAxis('bottom').setTicks([[(i, str(i / self.sampling_rate)) for i in range(0, self.num_points, int(self.sampling_rate / 2))] + [(self.num_points, str(self.num_points / self.sampling_rate))]])

        self.eeg_plot.setTitle('EEG Channels with Trigger')

        # Set a smaller vertical offset to fit within the reduced height
        self.offset_amplitude = 200  # Adjusted for smaller plot height
        self.trigger_offset = -self.offset_amplitude  # Offset for the trigger channel

        # Initialize the curves and quality indicators for each channel
        self.curves = []
        self.quality_indicators = []

        for i, channel in enumerate(self.eeg_channels):
            # Plot each channel with a different color
            curve = self.eeg_plot.plot(pen=colors[i])
            self.curves.append(curve)

            # Create and add quality indicator
            scatter = ScatterPlotItem(size=20, brush=mkBrush('green'))
            # position the item according to the offset for each channel
            scatter.setPos(-1, i * self.offset_amplitude)
            self.eeg_plot.addItem(scatter)
            self.quality_indicators.append(scatter)

            # Add labels for each channel
            text_item = pg.TextItem(text=layouts[self.board_id]["channels"][i], anchor=(1, 0.5))
            text_item.setPos(-10, i * self.offset_amplitude)  # Position label next to the corresponding channel
            self.eeg_plot.addItem(text_item)

            # Add a small indicator for the uV range next to each channel
            uv_indicator = pg.TextItem(text=f"±{int(self.offset_amplitude / 2)} uV", anchor=(0, 1))
            uv_indicator.setPos(-10, i * self.offset_amplitude)  # Position the indicator on the right side
            #self.eeg_plot.addItem(uv_indicator)

        # Add the trigger curve at the bottom
        trigger_curve = self.eeg_plot.plot(pen='red')
        self.curves.append(trigger_curve)

        # Add label for the trigger channel
        trigger_label = pg.TextItem(text="Trigger", anchor=(1, 0.5))
        trigger_label.setPos(-10, self.trigger_offset)  # Position label next to the trigger channel
        self.eeg_plot.addItem(trigger_label)
        return self.eeg_plot

    def update_plot(self):
        """Update the plot with new data."""
        filtered_eeg = np.zeros((len(self.eeg_channels) + 1, self.num_points))
        if self.is_streaming:
            if self.window_size == 0:
                raise ValueError("Window size cannot be zero")
            data = self.board.get_current_board_data(num_samples=self.num_points)
            self.filter_data_buffer(data)
            start_eeg = layouts[self.board_id]["eeg_start"]
            end_eeg = layouts[self.board_id]["eeg_end"]
            eeg = data[start_eeg:end_eeg]

            for count, channel in enumerate(self.eeg_channels):
                ch_data = eeg[count]
                filtered_eeg[count] = ch_data

                if self.plot:
                    # Apply the offset for display purposes only
                    ch_data_offset = ch_data + count * self.offset_amplitude
                    self.curves[count].setData(ch_data_offset)

            # Plot the trigger channel, scaled and offset appropriately
            trigger = data[-1] * 100
            #filtered_eeg[-1] = trigger
            if self.plot:
                # Rescale trigger to fit the display range and apply the offset
                trigger_rescaled = (trigger * (self.offset_amplitude / 5.0) + self.trigger_offset)
                self.curves[-1].setData(trigger_rescaled.tolist())

            # Adjust the Y range to fit all channels with their offsets and the trigger
            min_display = self.trigger_offset - self.offset_amplitude
            max_display = (len(self.eeg_channels) - 1) * self.offset_amplitude + np.max(eeg)
            self.eeg_plot.setYRange(min_display, max_display)

            self.update_quality_indicators(filtered_eeg, push=True)
            self.app.processEvents()

    def export_file(self, filename=None, folder='export', format='csv'):
        """
        Export the data to a file
        :param filename: str, name of the file
        :param folder: str, name of the folder
        :param format: str, format of the file
        """
        # Compose the file name using the board name and the current time
        try:
            if filename is None:
                filename = f"{self.board.get_device_name(self.board_id)}_{time.strftime('%Y-%m-%d_%H-%M-%S')}"
            path = os.path.join(folder, filename + '.' + format)
            if not os.path.exists(folder):
                os.makedirs(folder)
            with open(path, 'w') as self.file:
                write_header(self.file, self.board_id)
                data = self.board.get_board_data()
                if format == 'csv':
                    DataFilter.write_file(data, path, 'a')
        except Exception as e:
            logging.error(f"Error exporting file: {e}")

    def write_trigger(self, trigger=1, timestamp=0):
        """
        Insert a trigger into the data stream
        :param trigger: int, trigger value
        :param timestamp: float, timestamp value
        """
        if trigger == '':
            logging.error("Trigger value cannot be empty")
            return
        if timestamp == 0:
            timestamp = time.time()
        self.board.insert_marker(int(trigger))
        if self.prediction_mode:
            if int(trigger) == self.nclasses and not self.first_prediction:  # half way trial
                self.predict_class()
            elif int(trigger) == self.nclasses and self.first_prediction:
                logging.debug('Skipping first prediction')
                self.first_prediction = False

    def init_classifier(self):
        """ Initialize the classifier """
        self.classifier = Classifier(model=self.model, board_id=self.board_id)

    def set_train_start(self):
        """" Set the start of the training"""
        self.start_training_time = time.time()

    def train_classifier(self):
        """ Train the classifier"""
        end_training_time = time.time()
        training_length = end_training_time - self.start_training_time + 1
        training_interval = int(training_length * self.sampling_rate)
        logging.info(f"Training duration: {training_length}")
        data = self.board.get_current_board_data(training_interval)
        self.filter_data_buffer(data)
        self.classifier.train(data, oversample=self.over_sample)

    def start_prediction(self):
        """Start the prediction timer."""
        self.prediction_timer.start(self.prediction_interval)

    def stop_prediction(self):
        """Stop the prediction timer."""
        self.prediction_timer.stop()

    def _predict_class(self, data):
        """Internal method to predict the class of the data."""
        try:
            output = self.classifier.predict(data, proba=True, group=True)
            push_lsl_prediction(self.prediction_outlet, output)
            logging.info(f"Predicted class: {output}")
        except Exception as e:
            logging.error(f"Error predicting class: {e}")

    def predict_class(self):
        """Predict the class of the data."""
        try:
            data = self.board.get_current_board_data(self.prediction_datapoints)
            self.filter_data_buffer(data)
            self.executor.submit(self._predict_class, data)
        except Exception as e:
            logging.error(f"Error starting prediction task: {e}")

    def update_quality_indicators(self, sample, push=False):
        """ Update the quality indicators for each channel"""
        eeg_start = layouts[self.board_id]["eeg_start"]
        eeg_end = layouts[self.board_id]["eeg_end"]
        eeg = sample[eeg_start:eeg_end]
        amplitudes = []
        q_colors = []
        q_scores = []
        for i in range(len(eeg)):
            amplitude_data = eeg[i]  # get the data for the i-th channel
            color, amplitude, q_score = self.get_channel_quality(amplitude_data)
            q_colors.append(color)
            amplitudes.append(np.round(amplitude, 2))
            q_scores.append(q_score)
            # Update the scatter plot item with the new color
            self.quality_indicators[i].setBrush(mkBrush(color))
            self.quality_indicators[i].setData([-1], [0])  # Position the circle at (0, 0)
        if push:
            push_lsl_quality(self.quality_outlet, q_scores)
        logging.debug(f"Qualities: {q_scores} {q_colors}")

    def get_channel_quality(self, eeg, threshold=75):
        """ Get the quality of the EEG channel based on the amplitude"""
        amplitude = np.percentile(eeg, threshold)
        q_score = 0
        color = 'red'
        for low, high, color_name, score in self.quality_thresholds:
            if low <= amplitude <= high:
                color = color_name
                q_score = score
                break
        return color, amplitude, q_score

    def filter_data_buffer(self, data):
        start_eeg = layouts[self.board_id]["eeg_start"]
        end_eeg = layouts[self.board_id]["eeg_end"]
        eeg = data[start_eeg:end_eeg]
        for count, channel in enumerate(self.eeg_channels):
            ch_data = eeg[count]
            if self.bandpass_checkbox.isChecked():
                start_freq = float(self.bandpass_box_low.text()) if self.bandpass_box_low.text() != '' else 0
                stop_freq = float(self.bandpass_box_high.text()) if self.bandpass_box_high.text() != '' else 0
                self.apply_bandpass_filter(ch_data, start_freq, stop_freq)
            if self.notch_checkbox.isChecked():
                freqs = np.array(self.notch_box.text().split(','))
                self.apply_notch_filter(ch_data, freqs)

    def apply_bandpass_filter(self, ch_data, start_freq, stop_freq, order=4,
                              filter_type=FilterTypes.BUTTERWORTH_ZERO_PHASE, ripple=0):
        DataFilter.detrend(ch_data, DetrendOperations.CONSTANT.value)
        if start_freq >= stop_freq:
            logging.error("Start frequency should be less than stop frequency")
            return
        if start_freq < 0 or stop_freq < 0:
            logging.error("Frequency values should be positive")
            return
        if start_freq > self.sampling_rate / 2 or stop_freq > self.sampling_rate / 2:
            logging.error(
                "Frequency values should be less than half of the sampling rate in respect of Nyquist theorem")
            return
        try:
            DataFilter.perform_bandpass(ch_data, self.sampling_rate, start_freq, stop_freq, order, filter_type, ripple)
        except ValueError as e:
            logging.error(f"Invalid frequency value {e}")

    def apply_notch_filter(self, ch_data, freqs, bandwidth=2.0, order=4, filter_type=FilterTypes.BUTTERWORTH_ZERO_PHASE,
                           ripple=0):
        for freq in freqs:
            if float(freq) < 0:
                logging.error("Frequency values should be positive")
                return
            if float(freq) > self.sampling_rate / 2:
                logging.error("Frequency values should be less than half of the sampling rate in respect of Nyquist "
                              "theorem")
                return
        try:
            for freq in freqs:
                start_freq = float(freq) - bandwidth
                end_freq = float(freq) + bandwidth
                DataFilter.perform_bandstop(ch_data, self.sampling_rate, start_freq, end_freq, order,
                                            filter_type, ripple)
        except ValueError:
            logging.error("Invalid frequency value")

    def toggle_stream(self):
        """ Start or stop the streaming of data"""
        if self.is_streaming:
            self.board.stop_stream()
            self.start_button.setText('Start')
            self.is_streaming = False
        else:
            self.board.start_stream()
            self.start_button.setText('Stop')
            self.is_streaming = True

    def quit(self):
        """ Quit the application, join the threads and stop the streaming"""
        if self.save_data_checkbox.isChecked():
            self.export_file()
        self.lsl_thread.quit()
        self.classifier_thread.join()
        self.board.stop_stream()
