import os
import logging
import traceback
import shutil
import zipfile
import random
import re
import numpy as np
import mne
import warnings
import torch
import warnings
import torch
from tqdm import tqdm

from .wavelet_filtering import Wavelet_Transformer
class DatasetDownloader:
    """
    A class to handle downloading and preparing datasets for processing.

    Methods
    -------
    __init__(downloadAndPrepareImmediately=True, processImmediately=True, processingMode='raw', downloadURLs=None, datasetPath=None, logger=None, verbose=False)
        Initializes the DatasetDownloader with optional immediate download and processing.

    startDownloadingAndPrepare()
        Initiates the downloading and preparation of the dataset. (Call if downloadAndPrepareImmediately = False)
    """
    def __init__(self, downloadAndPrepareImmediately = True, processImmediately = True,**kwargs):
        defaultDownloadURLs = [ ## links to normalized data of all participants
            "https://s3.amazonaws.com/openneuro/ds000117/ds000117_R1.0.0/compressed/ds000117_R1.0.0_derivatives_sub01-04.zip",
            "https://s3.amazonaws.com/openneuro/ds000117/ds000117_R1.0.0/compressed/ds000117_R1.0.0_derivatives_sub05-08.zip",
            "https://s3.amazonaws.com/openneuro/ds000117/ds000117_R1.0.0/compressed/ds000117_R1.0.0_derivatives_sub09-12.zip",
            "https://s3.amazonaws.com/openneuro/ds000117/ds000117_R1.0.0/compressed/ds000117_R1.0.0_derivatives_sub13-16.zip"
        ]
        passedDownloadURLs = kwargs.get('downloadURLs', defaultDownloadURLs)
        self.downloadURLs = passedDownloadURLs if passedDownloadURLs else defaultDownloadURLs

        self.datasetPath = kwargs.get('datasetPath', os.path.join('/srv','openfmri'))

        self.logger = kwargs.get('logger', None)
        self.verbose = kwargs.get('verbose', False)

        self.processingMode = kwargs.get("processingMode",'raw')

        if self.logger is None:
            logging.basicConfig(level=logging.INFO)
            self.logger = logging.getLogger(__name__)

        if self.verbose:
            self.logger.setLevel(logging.DEBUG)

        if downloadAndPrepareImmediately:
            if os.path.exists(os.path.join(self.datasetPath,'.randomized')) \
               and os.path.exists(os.path.join(self.datasetPath,'.arranged')) \
               and os.path.exists(os.path.join(self.datasetPath,'.unzipped')) \
               and os.path.exists(os.path.join(self.datasetPath,'.downloaded')) \
               and os.path.exists(os.path.join(self.datasetPath,'.sharded')) \
               and os.path.exists(os.path.join(self.datasetPath,'.waveleted')):
                self.logger.info("Skipping downloading and preparing... (Found .downloaded, .unzipped, .arranged, .randomized files)")
            else:
                self.startDownloadingAndPrepare()
        if processImmediately:
            if os.path.exists(os.path.join(self.datasetPath,'.processed')):
                self.logger.info("Skipping processing...")
            else:
                processer = DatasetPreprocesser(
                    datasetPath = self.datasetPath, 
                    processImmediately = True, 
                    mode = self.processingMode, 
                    logger = self.logger,
                    verbose = self.verbose
                )

    def startDownloadingAndPrepare(self):
        assert self.downloadURLs is not None and len(self.downloadURLs)>0, f"No download URLs specified. (Given {self.downloadURLs})"
        self.logger.info(f"Downloading and processing entire dataset using URLs: {self.downloadURLs}")
        self._downloadDataset()
        self._prepareDataset()
        return True

    def _downloadDataset(self, datasetPath=None, downloadURLs=None):
        """
        Downloads the dataset from the specified URLs to the given dataset path.

        Parameters:
        - datasetPath (str, optional): The directory path where the dataset will be downloaded.
            Defaults to the instance's datasetPath attribute if not provided.
        - downloadURLs (list, optional): A list of URLs from which to download the dataset.
            Defaults to the instance's downloadURLs attribute if not provided.

        This method checks if the dataset path exists and creates it if it doesn't.
        It then iterates over the download URLs, downloading each file if it doesn't
        already exist in the dataset path. Successfully downloaded files are logged.
        """
        if datasetPath is None:
            datasetPath = self.datasetPath
        if downloadURLs is None:
            downloadURLs = self.downloadURLs
        if not os.path.exists(datasetPath):
            os.makedirs(datasetPath)
        
        self.downloadedFolders = []
        downloaded_marker = os.path.join(datasetPath, '.downloaded')
        
        if not os.path.exists(downloaded_marker):
            self.logger.info(f"Downloading {len(downloadURLs)} files...")
            for url in downloadURLs:
                file_name = os.path.join(datasetPath, url.split('/')[-1])
                if not os.path.exists(file_name):
                    self.logger.info(f"Downloading {file_name}...")
                    try:
                        os.system(f"wget -O {file_name} {url}")
                        self.downloadedFolders.append(file_name)
                    except Exception as e:
                        self.logger.error(f"Error downloading file {file_name}, {e}")
                        traceback.print_exc()
                else:
                    self.logger.info(f"{file_name} already exists, skipping download.")
            
            if len(self.downloadedFolders) == len(downloadURLs):
                with open(downloaded_marker, 'w') as f:
                    f.write('Download completed successfully.')
                self.logger.info(f"Successfully downloaded {len(self.downloadedFolders)} files.")
        else:
            self.logger.info("Dataset already downloaded. Skipping download.") 


    def _prepareDataset(self):
        """
        Iterates through downloaded folders (each holding part of the dataset) 
        and the participant folders within them. Removes all MRI data and moves 
        participant folders to one dataset folder (specified in self.datasetPath).             
        """
        self.participantCount = 0
        datasetFolder = self.datasetPath
        #### unzips folders in datasetfolder (after downloading earlier)
        self._unzipAndRenameInFolder(datasetFolder)
        ## rearranges and randomizes (on a subject level)
        self._arrangeFolders(datasetFolder)
        self._randomizeSubjectData(datasetFolder)

    
    def _unzipAndRenameInFolder(self, folder, remove=False):
        """
        Unzips all zip files in the specified folder and renames the extracted folders.

        This method checks if the folder has already been unzipped by looking for a marker file.
        If not, it unzips all zip files in the folder, optionally removes the zip files after extraction,
        and renames the extracted folders to a standardized format.

        Parameters:
        folder (str): The path to the folder containing zip files to be unzipped.
        remove (bool): If True, the zip files will be deleted after extraction. Defaults to False.

        Returns:
        None
        """
        unzipped_marker = os.path.join(folder, '.unzipped')
        if os.path.exists(unzipped_marker):
            self.logger.info(f"Folder {folder} is already unzipped. Exiting early.")
            return

        assert all(f.endswith('.zip') or f.startswith('.') for f in os.listdir(folder)), f"Not all files in {folder} are zip files or ignored files, please delete non-zip files and re-run."
        zip_file_count = sum(1 for f in os.listdir(folder) if f.endswith('.zip'))
        self.logger.info(f"Unzipping and renaming {zip_file_count} files in folder: {folder}")
        for zipFile in os.listdir(folder):
            ## unzip
            zipFile = os.path.join(folder, zipFile)
            if zipFile.endswith('.zip'):
                self.logger.info(f"Unzipping {zipFile}...")
                try:
                    with zipfile.ZipFile(zipFile, 'r') as zip_ref:
                        zip_ref.extractall(os.path.dirname(zipFile))
                    if remove:
                        os.remove(zipFile)  # Remove the zip file after extraction
                        self.logger.info(f"Unzipped and removed {zipFile}")
                    else:
                        self.logger.info(f"Unzipped {zipFile}")
                except Exception as e:
                    self.logger.error(f"Error unzipping {zipFile}, {e}")
                    traceback.print_exc()

        for i, unzippedFolder in enumerate(f for f in os.listdir(folder) if not f.endswith('.zip') and not f.startswith('.') and os.path.isdir(os.path.join(folder, f))):
            self.logger.debug(f"{i}, {unzippedFolder}")
            fromName = os.path.join(folder, unzippedFolder)
            toName = os.path.join(folder, f'folder_{i}')
            os.rename(fromName, toName)

        # Create the .unzipped marker file
        with open(unzipped_marker, 'w') as marker_file:
            marker_file.write('')


    def _arrangeFolders(self, datasetFolder):
        """
        Rearranges the files in the datasetFolder after downloading and unzipping.
        
        This method organizes the dataset by moving participant folders into the main dataset folder,
        ensuring that relevant files are not nested within subdirectories. It prepares the dataset
        for randomization and later access by ensuring a consistent folder structure.

        Args:
            datasetFolder (str): The path to the main dataset folder where the unzipped files are located.
        
        Raises:
            AssertionError: If the number of participant folders does not match the expected count.
        """
        arranged_marker = os.path.join(datasetFolder, '.arranged')
        if os.path.exists(arranged_marker):
            self.logger.info(f"Dataset folder {datasetFolder} is already arranged. Exiting early.")
            return

        self.participantCount = 0
        unzippedFolders = [f for f in os.listdir(datasetFolder) if os.path.isdir(os.path.join(datasetFolder, f))]
        self.logger.debug(f"Unzipped folders found: \n {unzippedFolders} \nRearranging.")
        
        for folder in unzippedFolders:
            ### move participant folders into dataset folder (so they are not nested)
            self.logger.debug(f"Moving subjects in {folder} to parent ({datasetFolder}) and deleting.")
            desiredSubFolder = os.path.join(folder, 'derivatives', 'meg_derivatives')
            self.logger.debug(f"sub:{desiredSubFolder},  parent:{datasetFolder}")
            DatasetDownloader.moveContentsToParentAndDeleteSub(datasetFolder, desiredSubFolder,folderOnly = True)
        
        ### go through participant folders and move nested relevant files upwards to participant folder
        participantFolders = [f for f in os.listdir(datasetFolder) if not f.startswith('.') and not f.endswith('.zip')]
        self.logger.debug(f"Participant folders found: \n {participantFolders} \nRearranging.")
        for participantFolder in participantFolders:
            self.participantCount += 1
            desiredSubFolder = os.path.join('ses-meg', 'meg')
            participantFolderPath = os.path.join(datasetFolder,participantFolder)
            self.logger.debug(f"Moving data in {participantFolder} to parent ({datasetFolder}) and deleting.")
            self.logger.debug(f"sub:{desiredSubFolder},  parent:{datasetFolder}")
            DatasetDownloader.moveContentsToParentAndDeleteSub(participantFolderPath,desiredSubFolder)


        ### go through participant folders again and renames fif files to standardized format
        participantFolders = [f for f in os.listdir(datasetFolder) if not f.startswith('.') and not f.endswith('.zip')]
        self.logger.debug(f"Participant folders: \n{participantFolders}")
        for participantFolder in participantFolders: 
            participantFolderPath = os.path.join(datasetFolder,participantFolder)
            for file in os.listdir(participantFolderPath):
                match = re.match(r'.*run-(0[1-6]).*', file)
                if match:
                    new_file_name = f'run_{match.group(1)}' + os.path.splitext(file)[1]
                    os.rename(os.path.join(participantFolderPath, file), os.path.join(participantFolderPath, new_file_name))

        ### at this stage dataset folder should contain n folders (with n being number of participants)
        non_zip_non_dot_folders = [f for f in os.listdir(datasetFolder) if not f.startswith('.') and not f.endswith('.zip')]
        assert len(non_zip_non_dot_folders) == self.participantCount, f"ERROR: Dataset folder contains {len(non_zip_non_dot_folders)} folders, but {self.participantCount} (participant) folders are expected."

        # Create the .arranged marker file
        with open(arranged_marker, 'w') as marker_file:
            marker_file.write('')

            
    def _randomizeSubjectData(self,datasetFolder,train_percentage=70, val_percentage=20):
        """
        Randomizes and splits participant data into training, validation, and test sets.

        This method shuffles the participant folders and divides them into three subsets:
        training, validation, and test, based on the specified percentages. The folders
        are then moved into corresponding subdirectories within the dataset path.

        Args:
            train_percentage (int): The percentage of participant data to allocate to the training set.
            val_percentage (int): The percentage of participant data to allocate to the validation set.

        Raises:
            ValueError: If the sum of train_percentage and val_percentage exceeds 100.
        """
        randomized_marker = os.path.join(datasetFolder, '.randomized')

        if not os.path.exists(randomized_marker):
            ### split participant folders into train, test, and val subfolders        
            participantFolders = [f for f in os.listdir(datasetFolder) if os.path.isdir(os.path.join(datasetFolder,f))]
            
            random.seed(42)
            random.shuffle(participantFolders)

            total_participants = len(participantFolders)
            train_percentage = int(train_percentage)
            val_percentage = int(val_percentage)
            train_count = int(total_participants * train_percentage / 100)
            val_count = int(total_participants * val_percentage / 100)

            train_folder = os.path.join(datasetFolder, 'train')
            val_folder = os.path.join(datasetFolder, 'val')
            test_folder = os.path.join(datasetFolder, 'test')

            os.makedirs(train_folder, exist_ok=True)
            os.makedirs(val_folder, exist_ok=True)
            os.makedirs(test_folder, exist_ok=True)

            for i, participantFolder in enumerate(f for f in participantFolders):
                participantFolderPath = os.path.join(datasetFolder, participantFolder)
                if i < train_count:
                    shutil.move(participantFolderPath, train_folder)
                elif i < train_count + val_count:
                    shutil.move(participantFolderPath, val_folder)
                else:
                    shutil.move(participantFolderPath, test_folder)

            # Create the .randomized marker file
            with open(randomized_marker, 'w') as marker_file:
                marker_file.write('')
        else:
            self.logger.info("Dataset has already been randomized. Skipping randomization step.")

    @staticmethod
    def moveContentsToParentAndDeleteSub(parentfolder, intermediateFolders, expectedContentCount = None, folderOnly = False):
        """
        Moves contents from intermediate folder to parent folder and deletes the intermediate folders.
        E.g.
            parentFolder: "data/openfmri"
            intermediateFolders: "folder_0/derivatives/meg_derivatives" (containing subject folders)
            --> moves all subject folders to parent folder
            
        Parameters:
        subfolder (str): The name (relative path) of the subfolder whose contents are to be moved.
        parentfolder (str): The absolute path to the parent folder where contents will be moved.

        Returns:
        None
        """
        intermediateFolderPath = os.path.join(parentfolder, intermediateFolders)
        if not os.path.exists(intermediateFolderPath):
            return
        if expectedContentCount is not None:
            actualContentCount = len(os.listdir(intermediateFolderPath))
            assert actualContentCount == expectedContentCount, (
                f"Expected {expectedContentCount} items, but found {actualContentCount} in {intermediateFolderPath}"
            )
        # Move contents of subfolder to parentfolder
        for item in os.listdir(intermediateFolderPath):
            item_path = os.path.join(intermediateFolderPath, item)
            if (folderOnly and os.path.isdir(item_path)) or not folderOnly:
                shutil.move(item_path, parentfolder)

        # Remove the folder out of which the contents were moved upwards
        normalized_path = os.path.normpath(intermediateFolders)
        outermost_folder = normalized_path.split(os.sep)[0]
        outermost_folder_path = os.path.join(parentfolder,outermost_folder)
        
        shutil.rmtree(outermost_folder_path)


class DatasetPreprocesser():
    def __init__(self,**kwargs):
        self.datasetPath = kwargs.get('datasetPath', os.path.join('/srv','openfmri'))

        self.logger = kwargs.get('logger', None)
        self.verbose = kwargs.get('verbose', False)

        if self.logger is None:
            logging.basicConfig(level=logging.INFO)
            self.logger = logging.getLogger(__name__)

        if self.verbose:
            self.logger.setLevel(logging.DEBUG)

        self.mode = str(kwargs.get('mode', "raw")).lower()


        self.windowLength_ms = kwargs.get('windowLength', 250)
        self.samplingRate_hz = kwargs.get('samplingRate', 1100)

        self.windowLength = int(self.windowLength_ms * self.samplingRate_hz / 1000)

        self._checkDatasetIntegrity(self.datasetPath)
        self._meanPoolData(self.datasetPath)
        self._makeTensorShards(self.datasetPath)
        
    def _checkDatasetIntegrity(self, datasetPath):
        """
        Checks the integrity of the dataset by ensuring that the dataset folder
        contains only directories for subjects and that each subject directory
        contains only allowed files and directories.

        Parameters:
        - datasetPath (str): The path to the dataset directory to be checked.

        Returns:
        - bool: True if the dataset integrity is confirmed, raises an assertion error otherwise.
        """
        ### iterate through mode folders (train, test, val)
        for modeFolder in os.listdir(datasetPath):
            if ".zip" in modeFolder or modeFolder[0] == ".":
                continue
            assert os.path.isdir(os.path.join(datasetPath,modeFolder)), f"Dataset folder contains unexpected file: {modeFolder}"
            assert modeFolder == "train" or modeFolder == "val" or modeFolder == "test", f"Dataset folder contains unexpected folder: {modeFolder} (expected train, test and val)"
            ### iterate through subject folders in dataset folder
            for subjectFolder in os.listdir(os.path.join(datasetPath, modeFolder)):
                assert os.path.isdir(os.path.join(datasetPath,modeFolder,subjectFolder)), f"Dataset folder contains unexpected file: {modeFolder}/{subjectFolder}"
                assert len(os.listdir(os.path.join(datasetPath,modeFolder,subjectFolder)))>0, f"Subject folder {modeFolder}/{subjectFolder} unexpectedly empty"
                ### iterate through run files in subject folders
                for file in os.listdir(os.path.join(datasetPath,modeFolder,subjectFolder)):
                    # Allow .txt files, .fif files, and EEG/MAG shard directories
                    is_valid = (
                        ".txt" in file or 
                        ".fif" in file or 
                        file == "EEG_shards" or 
                        file == "MAG_shards" or
                        file == "EEG_WAVELET_shards" or 
                        file == "MAG_WAVELET_shards"     
                    )
                    assert is_valid, f"Unexpected file/directory {file} in folder {modeFolder}/{subjectFolder}"
        return True
    

    def  _meanPoolData(self, datasetPath):
        """
        Handles mean pooling and processing of .fif files in the dataset.
        Parameters:
            data (numpy array): EEG/MEG data array with shape (n_channels, n_times).
            pool_size (int): The size of the pooling window. Defaults to 5.
        Returns:
            Processed and mean-pooled data, along with saved updates to .fif files.
        """

        pooled_marker = os.path.join(datasetPath, '.pooled')
        if os.path.exists(pooled_marker):
            return
                
        warnings.filterwarnings("ignore",message=".* does not conform to MNE naming conventions.*",category=RuntimeWarning,)
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        mne.set_log_level("WARNING")

        def _mean_pool(data, pool_size=5):
            """
            Reduces the temporal resolution by averaging every 'pool_size' samples.
            Parameters:
                data (numpy array): Shape (n_channels, n_times).
                pool_size (int): Number of samples to pool together.
            Returns:
                numpy array: Mean-pooled data with shape (n_channels, n_times // pool_size).
            """
            n_channels, n_times = data.shape
            remainder = n_times % pool_size
            if remainder != 0:
                data = data[:, : (n_times - remainder)]
            data = data.reshape(n_channels, -1, pool_size)
            data_pooled = data.mean(axis=-1)
            return data_pooled

        def _chunk_into_windows(data, window_size=275):
            """
            Divides data into sequential windows of a specified size.
            Parameters:
                data (numpy array): Shape (n_channels, n_times_pooled).
                window_size (int): Number of frames per window.
            Returns:
                numpy array: Data reshaped to (n_windows, n_channels, window_size).
            """
            n_channels, n_times = data.shape
            n_windows = n_times // window_size
            usable = n_windows * window_size
            data = data[:, :usable]
            data = data.reshape(n_channels, n_windows, window_size)
            data = np.transpose(data, (1, 0, 2))
            return data

        def _pool_chunk_and_overwrite(fif_path, pool_size=5, window_size=275):
            """
            Processes .fif files by mean pooling and windowing, then overwrites them.
            Parameters:
                fif_path (str): Path to the .fif file.
                pool_size (int): Size of the pooling window.
                window_size (int): Size of the data window after pooling.
            """
            try:
                logger.info(f"Processing & Overwriting: {fif_path}")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # Ignore MNE warnings
                    raw_original = mne.io.read_raw_fif(fif_path, preload=True)
                    original_data = raw_original.get_data()
                    original_sf = raw_original.info['sfreq']
            except Exception as e:
                logger.error(f"Failed to read {fif_path}")
                logger.error(f"Error: {str(e)}")
                return False

            try:
                # Mean pool
                data_pooled = _mean_pool(original_data, pool_size=pool_size)
                
                # Break into windows
                windows = _chunk_into_windows(data_pooled, window_size=window_size)
                n_windows = windows.shape[0]

                # Flatten windows
                if n_windows > 0:
                    flattened_data = windows.transpose(1, 0, 2).reshape(original_data.shape[0], -1)
                else:
                    flattened_data = np.zeros((original_data.shape[0], 0), dtype=np.float32)

                logger.info(
                    f"  Shapes:\n"
                    f"    Original: {original_data.shape},\n"
                    f"    After pooling: {data_pooled.shape},\n"
                    f"    #windows of {window_size} frames: {n_windows},\n"
                    f"    Final 'flattened' shape: {flattened_data.shape}."
                )

                ch_names = raw_original.ch_names
                ch_types = raw_original.get_channel_types()
                new_info = mne.create_info(
                    ch_names=ch_names, 
                    sfreq=(original_sf / pool_size), 
                    ch_types=ch_types
                )
                
                new_raw = mne.io.RawArray(flattened_data, new_info)
                new_raw.set_meas_date(raw_original.info['meas_date'])
                new_raw.info['bads'] = list(raw_original.info['bads'])

                new_raw.save(fif_path, overwrite=True)
                return True

            except Exception as e:
                logger.error(f"Error processing {fif_path}")
                logger.error(f"Error: {str(e)}")
                return False

        def _process_all(dataset_path):
            """
            Processes all .fif files in a dataset, organized by train/val/test.
            Parameters:
                dataset_path (str): Path to the dataset root.
                pool_size (int): Pooling window size.
                window_size (int): Window size after pooling.
            """
            processed_files = []
            failed_files = []

            for mode in ["train", "val", "test"]:
                mode_path = os.path.join(dataset_path, mode)
                if not os.path.isdir(mode_path):
                    logger.warning(f"Skipping non-existent folder: {mode_path}")
                    continue

                subjects = sorted(os.listdir(mode_path))
                logger.info(f"[{mode}] Found {len(subjects)} potential items in: {mode_path}")

                for subject in subjects:
                    subject_path = os.path.join(mode_path, subject)
                    if not os.path.isdir(subject_path):
                        continue

                    fif_files = [f for f in os.listdir(subject_path) if f.endswith('.fif')]
                    if not fif_files:
                        logger.info(f"No .fif files found for subject {subject}, skipping.")
                        continue

                    logger.info(f"Subject {subject} has {len(fif_files)} .fif runs.")
                    for run_file in fif_files:
                        fif_path = os.path.join(subject_path, run_file)
                        if _pool_chunk_and_overwrite(fif_path, pool_size=5, window_size=275):
                            processed_files.append(fif_path)
                        else:
                            failed_files.append(fif_path)

            pooled_marker = os.path.join(dataset_path, '.pooled')
            with open(pooled_marker, 'w') as marker_file:
                marker_file.write('')
            
            if failed_files:
                logger.warning(f"Processing completed with {len(failed_files)} corrupted files skipped")

        _process_all(datasetPath)


    def _makeTensorShards(self, datasetPath):
        """
        Reads raw data, chunks into windows, saves as shards

        Parameters:
            dataset_path (str): Path to the dataset root directory.
            window_size (int, optional): Size of each window in samples. Defaults to 275.
            shard_output_dir (str, optional): Directory to save the shards. If None, saves in subject directories.
            allow_padding (bool, optional): Whether to pad short sequences. Defaults to False.
        """
        pooled_marker = os.path.join(datasetPath, '.sharded')
        if os.path.exists(pooled_marker):
            return
        warnings.filterwarnings("ignore",message=".* does not conform to MNE naming conventions.*",category=RuntimeWarning,)
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        mne.set_log_level("WARNING")

        def _safe_read_fif(fif_path):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    raw = mne.io.read_raw_fif(fif_path, preload=True)
                return raw
            except Exception as e:
                self.logger.warning(f"Skipping corrupted file {fif_path}: {str(e)}")
                return None


        def _make_3d_windows(data_2d, window_size=275, allow_padding=False, mode="EEG"):
            """
            Convert 2D array (channels x timepoints) into 3D tensor (channels x window_size x num_windows)
            """
            n_channels, n_timepoints = data_2d.shape
            n_windows = n_timepoints // window_size

            if n_windows == 0:
                if allow_padding:
                    # Pad to make one full window
                    pad_amount = window_size - n_timepoints
                    data_2d = np.pad(data_2d, ((0, 0), (0, pad_amount)), mode='constant')
                    n_windows = 1
                else:
                    logger.warning(f"Skipping {mode} data: too short ({n_timepoints} < {window_size})")
                    return None

            # Truncate to full windows
            data_2d = data_2d[:, :n_windows * window_size]
            
            # Reshape to 3D
            data_3d = data_2d.reshape(n_channels, n_windows, window_size).transpose(0, 2, 1)
            return torch.from_numpy(data_3d)
        

        def _make_and_save_shards(dataset_path, window_size=275, shard_output_dir=None, allow_padding=False):
            """
            Create and save shards without normalization.
            """

            for mode in ["train", "val", "test"]:
                mode_path = os.path.join(dataset_path, mode)
                if not os.path.isdir(mode_path):
                    continue

                # Count total files first
                total_files = 0
                subjects = sorted(os.listdir(mode_path))
                for subject in subjects:
                    subject_path = os.path.join(mode_path, subject)
                    if not os.path.isdir(subject_path):
                        continue
                    run_files = [f for f in os.listdir(subject_path) if f.endswith(".fif")]
                    total_files += len(run_files)

                if total_files == 0:
                    continue

                # Create progress bar for this mode
                pbar = tqdm(total=total_files, desc=f"Computing shards for {mode}", bar_format='{desc:<30} {percentage:3.0f}%|{bar:50}{r_bar}')

                for subject in subjects:
                    subject_path = os.path.join(mode_path, subject)
                    if not os.path.isdir(subject_path):
                        continue

                    run_files = [f for f in os.listdir(subject_path) if f.endswith(".fif")]
                    for run_file in run_files:
                        run_path = os.path.join(subject_path, run_file)
                        raw = _safe_read_fif(run_path)
                        if raw is None:
                            pbar.update(1)
                            continue

                        data_all = raw.get_data()

                        # EEG
                        eeg_indices = mne.pick_types(raw.info, meg=False, eeg=True)
                        eeg_data = data_all[eeg_indices, :]
                        if eeg_data.size > 0:
                            shard_eeg = _make_3d_windows(
                                eeg_data,
                                window_size=window_size,
                                allow_padding=allow_padding,
                                mode="EEG"
                            )
                            if shard_eeg is not None:
                                if shard_output_dir:
                                    out_dir = os.path.join(shard_output_dir, mode, subject)
                                else:
                                    out_dir = os.path.join(subject_path, "EEG_shards")
                                os.makedirs(out_dir, exist_ok=True)

                                name_base, _ = os.path.splitext(run_file)
                                out_fname = f"{name_base}_eeg.pt"
                                out_path = os.path.join(out_dir, out_fname)

                                torch.save(shard_eeg, out_path)

                        # MAG
                        mag_indices = mne.pick_types(raw.info, meg='mag', eeg=False)
                        mag_data = data_all[mag_indices, :]
                        if mag_data.size > 0:
                            shard_mag = _make_3d_windows(
                                mag_data,
                                window_size=window_size,
                                allow_padding=allow_padding,
                                mode="MAG"
                            )
                            if shard_mag is not None:
                                if shard_output_dir:
                                    out_dir = os.path.join(shard_output_dir, mode, subject)
                                else:
                                    out_dir = os.path.join(subject_path, "MAG_shards")
                                os.makedirs(out_dir, exist_ok=True)

                                name_base, _ = os.path.splitext(run_file)
                                out_fname = f"{name_base}_mag.pt"
                                out_path = os.path.join(out_dir, out_fname)

                                torch.save(shard_mag, out_path)
                        pbar.update(1)
                pbar.close()

        
        def _main_shard_pipeline(dataset_path, window_size=275, shard_output_dir=None):
            """
            Main pipeline to create shards
            """
            _make_and_save_shards(
                dataset_path=dataset_path,
                window_size=window_size,
                shard_output_dir=shard_output_dir,
                allow_padding=False
            )

            pooled_marker = os.path.join(dataset_path, '.sharded')
            with open(pooled_marker, 'w') as marker_file:
                marker_file.write('')

        _main_shard_pipeline(dataset_path=datasetPath, window_size=275, shard_output_dir=None)


    def _runWaveletTransform(self, dataset_path):
        logger = logging.getLogger(__name__)
        wavelet_marker = os.path.join(dataset_path, '.wavelet')
        if os.path.exists(wavelet_marker):
            self.logger.info("Skipping wavelet transformation (found .wavelet marker)")
        else:
            self.logger.info("Creating wavelet transformed shards...")
            wavelet_transformer = Wavelet_Transformer(
                dataset_path=dataset_path,
                mode='all',
                eeg_channel=13,
                mag_channel=21
            )
            wavelet_transformer.process_wavelet_shards()
            # Create marker file after successful wavelet processing
            with open(wavelet_marker, 'w') as marker_file:
                marker_file.write('')