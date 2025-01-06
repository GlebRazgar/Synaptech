from dotenv import load_dotenv
import os
from torch.utils.data import DataLoader
# from models.unet import EEGtoMEGUNet #Uncomment if not using wavelet transform
from models.wavelet_unet import EEGtoMEGUNet # Comment out if not using wavelet transform
import matplotlib.pyplot as plt
import json
import torch
import torch.nn as nn
import torch.optim as optim
import logging
from tqdm import tqdm
import traceback
import signal
import sys
import time
import wandb
from dataset.shard_loader import ShardDataLoader
from dataset.dataset_builder import DatasetDownloader
import re  # Import the re module for regular expressions
from dataset.wavelet_filtering import Wavelet_Transformer

# Flag to indicate if termination has been requested
termination_requested = False

def signal_handler(signum, frame):
    global termination_requested    
    print(f"Received signal {signum}. Termination requested.")
    termination_requested = True
    raise KeyboardInterrupt 

def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Load training runs configuration
    with open('training_runs.json', 'r') as f:
        training_runs = json.load(f)

    for run_config in training_runs:
        # Obtain base run name
        base_run_name = run_config.get('name', 'default_run')

        # Prepare the runs directory
        runs_directory = 'runs'
        os.makedirs(runs_directory, exist_ok=True)

        # Get list of existing run directories
        existing_run_dirs = [d for d in os.listdir(runs_directory) if os.path.isdir(os.path.join(runs_directory, d))]

        # Initialize run_name
        run_name = base_run_name

        # Find existing runs with the same base name
        matching_runs = [d for d in existing_run_dirs if d.startswith(base_run_name)]

        if matching_runs:
            # Extract numerical suffixes
            suffixes = []
            for run_dir in matching_runs:
                # Match pattern: base_run_name followed by optional digits
                match = re.match(rf'^{re.escape(base_run_name)}(\d*)$', run_dir)
                if match:
                    suffix = match.group(1)
                    if suffix == '':
                        suffix_int = 0
                    else:
                        suffix_int = int(suffix)
                    suffixes.append(suffix_int)
            new_suffix = max(suffixes) + 1
            if new_suffix == 0:
                run_name = base_run_name
            else:
                run_name = f"{base_run_name}{new_suffix}"
        else:
            run_name = base_run_name

        # Update run_config['name'] with adjusted run_name
        run_config['name'] = run_name

        num_epochs = run_config.get('epochs', 10)
        files_percentage = run_config.get('files_percentage', 1.0)
        verbose = run_config.get('verbose', False)
        num_workers = run_config.get('num_workers', 4)
        batch_size = run_config.get('batch_size', 128)
        prefetch_factor = run_config.get('prefetch_factor', 3)
        model_weights_file = run_config.get('model_weights_file', '')
        learning_rate = run_config.get('learning_rate', 0.0001)
        task_mode = run_config.get('task_mode', 'gait')
        breakRun = run_config.get('break', False)

        if breakRun:
            break

        # Initialize W&B run
        wandb.init(project="Synaptech", 
                   name=run_name, 
                   config=run_config)

        log_folder_name = f'{runs_directory}/{run_name}'
        os.makedirs(log_folder_name, exist_ok=True)

        weight_file_set = False
        initialWeightsFile = f"{log_folder_name}/model_initial.pth"
        if model_weights_file:
            initialWeightsFile = model_weights_file
            weight_file_set = True

        def printConfig():
            logger.info("INITIALIZING TRAINING RUN")
            logger.info("Run Configuration:")
            logger.info(f"run_name: {run_name}")
            logger.info(f"num_epochs: {num_epochs}")
            logger.info(f"learning_rate: {learning_rate}")
            logger.info(f"model_weights_file: {model_weights_file}")
            logger.info(f"files_percentage: {files_percentage}")
            logger.info(f"verbose: {verbose}")
            logger.info(f"batch_size: {batch_size}")
            logger.info(f"num_workers: {num_workers}")
            logger.info(f"prefetch_factor: {prefetch_factor}")

        logging.basicConfig(filename=f'{log_folder_name}/training_log.log', level=logging.INFO, 
                            format='%(asctime)s - %(levelname)s - %(message)s')

        logger = logging.getLogger()

        # Suppress warnings from matplotlib font manager
        logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)

        if verbose:
            logger.setLevel(logging.DEBUG)

        # Print configuration
        printConfig()

        logger.info("Checking device..")
        device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
        logger.info(f"Using: {device}") 

        logger.info("Loading Data...")

        train_loader = None
        val_loader = None

        logger.info("Loading OpenFMRI dataset...")

        load_dotenv()
        dataset_path = os.getenv("DATASET_PATH")

        logger = logging.getLogger()

        #Downloads the dataset
        DatasetDownloader(downloadAndPrepareImmediately=True, datasetPath=dataset_path, processImmediately=True, processingMode='raw', logger=logger, verbose=False)
        
        #Performs wavelet frequency filtering

        Wavelet_Transformer(dataset_path=dataset_path, mode='all', eeg_channel=13, mag_channel=21)

        shard_data_loader_train = ShardDataLoader(dataset_path=dataset_path, mode='train', logger=logger, verbose=verbose, wavelet=True)
        shard_data_loader_val = ShardDataLoader(dataset_path=dataset_path, mode='val', logger=logger, verbose=verbose, wavelet=True)
        sample_length = 275 

        print ("i think its donezo")

        # Initialize model
        logger.info("Initializing model...")
        model = EEGtoMEGUNet()
        model = model.to(device)

        logger.info("Initializing loss function & optimizer...")
        criterion = nn.MSELoss()
        optimizer = optim.Adam(params=model.parameters())

        epoch_stats = None

        train_losses = []
        val_losses = []
        train_accuracies = []
        val_accuracies = []
        train_f1_scores = []
        val_f1_scores = []

        y_true = []
        y_scores = []

        # Load initial weights file
        if not os.path.isfile(initialWeightsFile) and weight_file_set:
            logger.error(f"Provided model weights file {initialWeightsFile} does not exist. Exiting.")
            sys.exit(1)
        elif not os.path.isfile(initialWeightsFile):
            logger.warning(f"No initial model weights file {initialWeightsFile} found. Continuing training from scratch.")
            torch.save(model.state_dict(), initialWeightsFile)
            logger.info(f"Initial model weights saved to {initialWeightsFile}")
        else:
            logger.info(f"Loading model weights from {initialWeightsFile}")
            model.load_state_dict(torch.load(initialWeightsFile, map_location=device))
            logger.info(f"Model initialized with weights from {initialWeightsFile}")

        logger.info(f"Training for {num_epochs} epochs...")
        try:
            for epoch in range(num_epochs):
                if termination_requested:
                    logger.warning("Termination requested. Exiting outer training loop.")
                    break

                # Prepare DataLoaders for this epoch
                logger.info("Preparing training data for epoch...")
                train_dataset = shard_data_loader_train.prepare_epoch_dataset(sample_length=sample_length)
                train_loader = DataLoader(train_dataset, shuffle=True, batch_size=batch_size, num_workers=num_workers, prefetch_factor=prefetch_factor, pin_memory=device.type == 'cuda')

                logger.info("Preparing validation data for epoch...")
                val_dataset = shard_data_loader_val.prepare_epoch_dataset(sample_length=sample_length)
                val_loader = DataLoader(val_dataset, shuffle=False, batch_size=batch_size, num_workers=num_workers, prefetch_factor=prefetch_factor, pin_memory=device.type == 'cuda')

                # Training loop
                running_loss = 0.0
                model.train()  

                progress_bar = tqdm(total=len(train_loader), desc=f"Epoch [{epoch+1}/{num_epochs}] - Training - started {time.strftime('%H:%M')}", leave=True)

                for batch_idx, (inputs, labels) in enumerate(train_loader):
                    if termination_requested:
                        logger.warning("Termination requested. Exiting inner training loop.")
                        break

                    progress_bar.update(1)

                    inputs = inputs.to(device).float()
                    labels = labels.to(device).float()

                    optimizer.zero_grad()
                    outputs = model(inputs)

                    loss = criterion(outputs, labels)
                    loss.backward()
                    optimizer.step()
                    running_loss += loss.item()

                    # Log batch metrics to W&B
                    wandb.log({
                        'batch_idx': batch_idx,
                        'batch_loss': loss.item(),
                    })

                progress_bar.close()

                # Calculate epoch's training loss
                train_loss = running_loss / len(train_loader)
                train_losses.append(train_loss)

                # Validation loop
                model.eval()  # Set the model to evaluation mode
                val_running_loss = 0.0

                with torch.no_grad():
                    progress_bar = tqdm(total=len(val_loader), desc=f"Epoch [{epoch+1}/{num_epochs}] - Validation - started {time.strftime('%H:%M')}", leave=False)
                    for inputs, labels in val_loader:
                        if termination_requested:
                            logger.warning("Termination requested. Exiting validation loop.")
                            break
                        inputs = inputs.to(device).float()
                        labels = labels.to(device).float()

                        outputs = model(inputs)
                        progress_bar.update(1)

                        loss = criterion(outputs, labels)
                        val_running_loss += loss.item()
                    progress_bar.close()

                # Calculate epoch's validation loss
                val_loss = val_running_loss / len(val_loader)
                val_losses.append(val_loss)

                # Log epoch metrics to W&B
                wandb.log({
                    'epoch': epoch,
                    'train_loss': train_loss,
                    'val_loss': val_loss
                })

                # Log and save training history and weights
                logger.info(f"EPOCH {epoch+1}: Val Loss: {val_loss:.4f}")

                if num_epochs > 20:
                    if (epoch + 1) % 4 == 0:
                        torch.save(model.state_dict(), f'{log_folder_name}/model_epoch_{epoch+1}.pth')
                elif num_epochs > 15:
                    if (epoch + 1) % 3 == 0:
                        torch.save(model.state_dict(), f'{log_folder_name}/model_epoch_{epoch+1}.pth')
                elif num_epochs > 10:
                    if (epoch + 1) % 2 == 0:
                        torch.save(model.state_dict(), f'{log_folder_name}/model_epoch_{epoch+1}.pth')
                else:
                    torch.save(model.state_dict(), f'{log_folder_name}/model_epoch_{epoch+1}.pth')

                with open(f'{log_folder_name}/training_history.json', 'w') as f:
                    json.dump({
                        'train_losses': train_losses,
                        'val_losses': val_losses
                    }, f)

            logger.info(f"TRAINING {num_epochs} EPOCHS COMPLETED")
        except Exception as e:
            torch.save(model.state_dict(), f'{log_folder_name}/model_epoch_{epoch+1}_error.pth')
            logger.error(f"Training stopped due to an error: {str(e)}")
            logger.error(traceback.format_exc())
        finally:
            wandb.finish()
            logger.info("Saving epoch statistics")
            if epoch_stats is not None:
                with open(os.path.join(log_folder_name, 'epoch_stats.json'), 'w') as f:
                    json.dump(epoch_stats, f, indent=4)

if __name__ == "__main__":
    main()