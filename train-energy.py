from pathlib import Path
import sys
sys.path.append('.')

from dataset import create_dataset
from model.UNet import UNet, UNet_Energy
from utils.engine import GaussianDiffusionTrainer, EnergyTrainer
from utils.tools import train_one_epoch, train_one_epoch_energy, load_yaml

import torch
from utils.callbacks import ModelCheckpoint

import joblib


def train(config):
	consume = config["consume"]
	if consume:
		cp = torch.load(config["consume_path"])
		config = cp["config"]
	print(config)
	
	device = torch.device(config["device"])
	loader, param_scaler, prop_scaler = create_dataset(**config["Dataset"])
	joblib.dump(param_scaler, 'checkpoint/param_scaler_energy.pkl')
	joblib.dump(prop_scaler, 'checkpoint/prop_scaler_energy.pkl')
	start_epoch = 1
	
	model = UNet_Energy(**config["ModelEnergy"]).to(device)
	optimizer = torch.optim.AdamW(model.parameters(), lr=config["lr"], weight_decay=1e-4)
	trainer = EnergyTrainer(model, **config["Trainer"]).to(device)
	
	model_checkpoint = ModelCheckpoint(**config["CallbackEnergy"])
	
	if consume:
		model.load_state_dict(cp["model"])
		optimizer.load_state_dict(cp["optimizer"])
		model_checkpoint.load_state_dict(cp["model_checkpoint"])
		start_epoch = cp["start_epoch"] + 1
	
	for epoch in range(start_epoch, config["epochs"] + 1):
		loss = train_one_epoch_energy(trainer, loader, optimizer, device, epoch)
		model_checkpoint.step(loss, model=model.state_dict(), config=config,
							  optimizer=optimizer.state_dict(), start_epoch=epoch,
							  model_checkpoint=model_checkpoint.state_dict())


if __name__ == "__main__":
    config = load_yaml("config.yml", encoding="utf-8")
    train(config)