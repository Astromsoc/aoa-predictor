"""
    Script to train models on predicting age of acquisition for words.
    
    ---

    Written & Maintained by: 
        Astromsoc
    Last Updated at:
        Apr 11, 2023
"""


import os
import json
import wandb
import argparse
import numpy as np
from tqdm import tqdm
from ruamel.yaml import YAML
yaml = YAML(typ='safe')

import torch
from torchsummaryX import summary
from torch.utils.data import DataLoader
from transformers import BertTokenizer

from src.utils import *
from src.models import *
from src.split import split_dataset



class Trainer:
    def __init__(self, cfgs: ParamsObject, model: nn.Module, tokenizer_name: str,
                 trn_loader: DataLoader, val_loader: DataLoader, device: str='cuda'):
        self.cfgs = cfgs
        self.model = model
        self.tokenizer_name = tokenizer_name
        self.trn_loader = trn_loader
        self.val_loader = val_loader
        self.bests = {'mae': float('inf'), 'epoch': -1}
        self.best_fps = list()
        self.criterion = nn.MSELoss()
        self.epoch = 1
        self.train_losses = list()
        self.train_maes = list()
        self.train_gradnorms = list()
        self.val_losses = list()
        self.val_maes = list()
        self.device = device
        self.use_wandb = False
        # init from cfgs
        self.init_from_cfgs()
        # take model to device
        self.model.to(self.device)
    

    def init_from_cfgs(self):
        self.scaler = torch.cuda.amp.GradScaler() if self.cfgs.scaler else None
        self.optimizer = torch.optim.AdamW(self.model.parameters(), **self.cfgs.optimizer.__dict__)
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            self.optimizer, **self.cfgs.scheduler.configs.__dict__
        ) if self.cfgs.scheduler.use else None


    def train_epoch(self):
        # build tqdm progress bar
        tqdm_bar = tqdm(total=len(self.trn_loader), leave=False, dynamic_ncols=True,
                        desc=f"training epoch [{self.epoch:<3}]")
        train_loss_this_epoch = np.zeros((len(self.trn_loader),))
        train_mae_this_epoch = np.zeros((len(self.trn_loader),))
        grad_norm = np.zeros((len(self.trn_loader),))
        # switch to training mode
        self.model.train()
        # iterate through batches
        for i, (ids, wlens, nsyls, ages) in enumerate(self.trn_loader):
            # take to device
            ids, wlens = ids.to(self.device), wlens.to(self.device)
            nsyls, ages = nsyls.to(self.device), ages.to(self.device)
            # obtain estimates & compute losses
            if self.scaler:
                with torch.cuda.amp.autocast():
                    pred_ages = self.model(ids, wlens, nsyls)
                    loss = self.criterion(pred_ages, ages)
            else:
                pred_ages = self.model(ids, wlens, nsyls)
                loss = self.criterion(pred_ages, ages)
            # compute mae
            train_mae_this_epoch[i] = (pred_ages - ages).norm(1) / len(pred_ages)
            train_loss_this_epoch[i] = loss.item()
            # backprop & update
            if self.scaler:
                with torch.cuda.amp.autocast():
                    self.scaler.scale(loss).backward()
                    self.scaler.step(self.optimizer)
                    self.scaler.update()
            else:
                loss.backward()
                self.optimizer.step()
            # compute gradient norm
            grad_norm[i] = sum([p.grad.data.detach().norm(2) 
                                for p in self.model.parameters() if p.grad is not None]) ** 0.5
            # clear grad
            self.optimizer.zero_grad()
            # update batch bar
            tqdm_bar.set_postfix(
                train_loss=f"{train_loss_this_epoch[i]:.6f}",
                train_mae=f"{train_mae_this_epoch[i]:.6f}",
                grad_norm=f"{grad_norm[i]:6f}",
                lr=self.optimizer.param_groups[0]['lr']
            )
            tqdm_bar.update()
            # push to wandb
            if self.use_wandb:
                wandb.log({"train_loss_per_batch": train_loss_this_epoch[i],
                           "train_mae_per_batch": train_mae_this_epoch[i],
                           "grad_norm_per_batch": grad_norm[i]})
        # clear
        del ids, wlens, nsyls, ages
        torch.cuda.empty_cache()
        tqdm_bar.close()

        return train_loss_this_epoch.mean(), train_mae_this_epoch.mean(), grad_norm.mean()
    

    def eval_epoch(self):
        tqdm_bar = tqdm(total=len(self.val_loader), leave=False, dynamic_ncols=True,
                        desc=f"eval epoch [{self.epoch:<3}]")
        val_loss_this_epoch = np.zeros((len(self.val_loader),))
        val_mae_this_epoch = np.zeros((len(self.val_loader),))
        # switch to inference mode
        self.model.eval()
        with torch.inference_mode():
            for i, (ids, wlens, nsyls, ages) in enumerate(self.val_loader):
                # take to device
                ids, wlens = ids.to(self.device), wlens.to(self.device)
                nsyls, ages = nsyls.to(self.device), ages.to(self.device)
                # obtain estimates
                if self.scaler:
                    with torch.cuda.amp.autocast():
                        pred_ages = self.model(ids, wlens, nsyls)
                else:
                    pred_ages = self.model(ids, wlens, nsyls)
                # compute mae
                val_mae_this_epoch[i] = (pred_ages - ages).norm(1) / len(pred_ages)
                # compute loss
                val_loss_this_epoch[i] = self.criterion(pred_ages, ages).item()
                # update batch bar
                tqdm_bar.set_postfix(
                    val_loss=f"{val_loss_this_epoch[i]:.6f}",
                    val_mae=f"{val_mae_this_epoch[i]:.6f}"
                )
                tqdm_bar.update()
        # clear
        del ids, wlens, nsyls, ages
        torch.cuda.empty_cache()
        tqdm_bar.close()

        return val_loss_this_epoch.mean(), val_mae_this_epoch.mean()
    

    def train(self, expcfgs: dict):
        """
            Train the current model for what's been specified in input experiment configs.
        """
        # if finetuning: load checkpoint
        if expcfgs.finetune.use:
            self.load_model(expcfgs.finetune.ckpt)

        while self.epoch <= expcfgs.epoch:
            train_avg_loss, train_avg_mae, train_avg_grad_norm = self.train_epoch()
            val_avg_loss, val_avg_mae = self.eval_epoch()
            # record
            self.train_losses.append(train_avg_loss)
            self.train_maes.append(train_avg_mae)
            self.train_gradnorms.append(train_avg_grad_norm)
            self.val_losses.append(val_avg_loss)
            self.val_maes.append(val_avg_mae)
            # update learning rate
            if self.scheduler: self.scheduler.step(self.val_losses[-1])
            # push to wandb
            if self.use_wandb:
                wandb.log({'train_loss_per_epoch': self.train_losses[-1],
                           'train_mae_per_epoch': self.train_maes[-1],
                           'train_gradnorm_per_epoch': self.train_gradnorms[-1],
                           'val_loss_per_epoch': self.val_losses[-1],
                           'val_mae_per_epoch': self.val_maes[-1],
                           'lr_per_epoch': self.optimizer.param_groups[0]['lr']})
            # save model
            self.save_model(expcfgs)
            # increment epoch by 1
            self.epoch += 1

    
    def save_model(self, expcfgs: str):
        """
            Save a model checkpoint to specified experiment folder.
        """
        # check if a lower val MSE is reached or the bests are not reached or it's the last epoch
        if (self.val_maes[-1] < self.bests['mae'] 
            or len(self.best_fps) < self.cfgs.max_saved_ckpts
            or self.epoch == expcfgs.epoch):
            # update best model stats
            if self.val_maes[-1] < self.bests['mae']:
                self.bests = {'mae': self.val_maes[-1], 'epoch': self.epoch}
            # sort the saved checkpoints (before reaching maximum storage)
            if len(self.best_fps) < self.cfgs.max_saved_ckpts:
                self.best_fps = [self.best_fps[i] for i in sorted(list(range(len(self.best_fps))), 
                                                                  key=lambda i: -self.val_losses[i])]
            # save checkpoint
            if len(self.best_fps) == self.cfgs.max_saved_ckpts:
                # delete the oldest checkpoint
                os.remove(self.best_fps.pop(0))
            # create folder if not existed
            if not os.path.exists(expcfgs.folder):
                os.makedirs(expcfgs.folder, exist_ok=True)
            # add new filepath
            if (self.epoch != expcfgs.epoch or self.bests['mae'] == self.val_maes[-1]):
                self.best_fps.append(os.path.join(expcfgs.folder, f"epoch-{self.epoch}.pt"))
            output_filepath = (self.best_fps[-1] if self.epoch != expcfgs.epoch 
                               else os.path.join(expcfgs.folder, f"epoch-{self.epoch}.pt"))
            # save model checkpoint
            torch.save({
                'model_state_dict': self.model.state_dict(),
                'optim_state_dict': self.optimizer.state_dict(),
                'epoch': self.epoch,
                'best_fps': self.best_fps,
                'bests': self.bests,
                'train_losses': self.train_losses,
                'train_maes': self.train_maes,
                'train_gradnorms': self.train_gradnorms,
                'val_losses': self.val_losses,
                'val_maes': self.val_maes,
                'configs': {'trainer': self.cfgs, 
                            'exp': expcfgs,
                            'tokenizer': self.tokenizer_name}
            }, output_filepath)
            print(f"\n[** MODEL SAVED **] Successfully saved checkpoint to [{self.best_fps[-1]}]\n")
    

    def load_model(self, ckpt_filepath: str):
        """
            Load a model checkpoint from specified filepath.
        """
        assert os.path.exists(ckpt_filepath), f"\n[** FILE NOT EXISTED **] Can't load from [{ckpt_filepath}].\n"
        loaded = torch.load(ckpt_filepath, map_location=torch.device(self.device))
        print(f"\n[** MODEL LOADED **] Successfully loaded checkpoint from [{ckpt_filepath}]\n")

        # load configs
        self.cfgs = loaded['configs']['trainer']
        # init from configs
        self.init_from_cfgs()
        # other state dicts / saved attributes
        self.model.load_state_dict(loaded['model_state_dict'])
        self.optimizer.load_state_dict(loaded['optim_state_dict'])
        self.epoch = loaded['epoch'] + 1
        self.best_fps = loaded['best_fps']
        self.bests = loaded['bests']
        self.train_losses = loaded['train_losses']
        self.train_maes = loaded['train_maes']
        self.train_gradnorms = loaded['train_gradnorms']
        self.val_losses = loaded['val_losses']
        self.val_maes = loaded['val_maes']




"""
    Main Driver Function
"""

def main(args):

    # load configurations
    cfgs = ParamsObject(yaml.load(open(args.config, 'r')))

    # fix random seeds
    np.random.seed(cfgs.seed)
    torch.manual_seed(cfgs.seed)

    # obtain device
    device = ('mps' if torch.backends.mps.is_available() else
              'cuda' if torch.cuda.is_available() else
              'cpu')
    print(f"\n[** DEVICE FOUND **] Now running on [{device}].\n")


    # split dataset
    if (not os.path.exists(cfgs.aoapred_train_filepath)
        or not os.path.exists(cfgs.aoapred_val_filepath)
        or not os.path.exists(cfgs.aoapred_test_filepath)):
        # build tokenizer
        TOKENIZER = BertTokenizer.from_pretrained(cfgs.tokenizer_name)
        # split dataset
        subset_filepaths = split_dataset(cfgs.aoa_csv_filepath, TOKENIZER)
        # output dataset
        for i, subname in enumerate('train val test'.split(' ')):
            # update configs
            cfgs.__dict__[f"aoapred_{subname}_filepath"] = subset_filepaths[i]
    
    # obtain chr2idx mapping for pretrained options
    if cfgs.model.choice == 'pretrained-emb':
        chr2idx = CharacterTokenizer(cfgs.model.chridx_filepath).chr2idx
        os.system(f"cp {cfgs.model.chridx_filepath} {cfgs.exp_configs.folder}/chr2idx.txt")
        # add num_chr & pad_idx to model configs
        cfgs.model.configs.num_chr = len(chr2idx)
        cfgs.model.configs.pad_idx = chr2idx['<pad>']
    
    # build datasets
    trainDataset = AoATrainDataset(filepath=cfgs.aoapred_train_filepath,
                                   pad_idx=chr2idx['<pad>'])
    valDataset   = AoATrainDataset(filepath=cfgs.aoapred_val_filepath,
                                   pad_idx=chr2idx['<pad>'])

    # build dataloaders
    trainLoader = DataLoader(dataset=trainDataset, 
                             collate_fn=trainDataset.collate_fn, 
                             **cfgs.train_loader.__dict__)
    valLoader = DataLoader(dataset=valDataset, 
                           shuffle=False, 
                           collate_fn=valDataset.collate_fn, 
                           **cfgs.val_loader.__dict__)

    # obtain the checkpoint model configs if continue training
    if cfgs.exp_configs.finetune.use:
        prev_expfolder = os.path.dirname(cfgs.exp_configs.finetune.ckpt)
        cfgs.model = ParamsObject(json.load(open(os.path.join(prev_expfolder, 'model-configs.json'), 'r')))
    
    # create folder if not existed
    if not os.path.exists(cfgs.exp_configs.folder):
        os.makedirs(cfgs.exp_configs.folder, exist_ok=True)

    # build model
    assert cfgs.model.choice in ChooseYourModel.keys(), (f"[** INVALID MODEL **] {cfgs.model.choice} "
                                         f"is provided. Please choose from: {ChooseYourModel.keys()}.")
    model = ChooseYourModel[cfgs.model.choice](**cfgs.model.configs.__dict__)

    # show model summary
    model.eval()
    ids, wlen, nsyl, _ = next(iter(trainLoader))
    with torch.inference_mode():
        print(summary(model, ids, wlen, nsyl))

    # build trainer
    trainer = Trainer(
        cfgs=cfgs.trainer_configs, 
        tokenizer_name=cfgs.tokenizer_name,
        model=model, 
        trn_loader=trainLoader,
        val_loader=valLoader, 
        device=device
    )

    # initiate wandb log tracker
    if cfgs.exp_configs.wandb.use:
        # add run name automatically
        cfgs.exp_configs.wandb.init_configs.name = (
            f"{cfgs.model.choice}-"
            f"interrim-dim-{cfgs.model.configs.interim_linear_dim}-"
            f"dropout-{cfgs.model.configs.dropout:.2f}-"
            f"wd-{cfgs.trainer_configs.optimizer.weight_decay:.2f}-"
            f"bs-{cfgs.train_loader.batch_size}-"
            f"lr-{cfgs.trainer_configs.optimizer.lr:.1e}-"
            f"{'with' if cfgs.model.configs.concat_wlen else 'no'}-wlen-"
            f"{'with' if cfgs.model.configs.concat_nsyl else 'no'}-nsyl"
        )
        if cfgs.exp_configs.anno: 
            cfgs.exp_configs.wandb.init_configs.name += f"-{cfgs.exp_configs.anno}" 
        wandb.init(config=cfgs, **cfgs.exp_configs.wandb.init_configs.__dict__)
        # revise experiment folder if name is specified
        cfgs.exp_configs.folder = cfgs.exp_configs.wandb.init_configs.name


    # copy model configs & other configs
    json.dump({'choice': cfgs.model.choice, 'configs': cfgs.model.configs.__dict__},
              open(os.path.join(cfgs.exp_configs.folder, 'model-configs.json'), 'w'),
              indent=4)
    os.system(f"cp {args.config} {cfgs.exp_configs.folder}/configs.yaml")

    # start training
    trainer.train(cfgs.exp_configs)
    



if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="Finetune a model to predict age of acquisition.")
    parser.add_argument(
        '--config', '-c', default='cfg/sample-train-configs.yaml', type=str,
        help="(str) Filepath to the configuration."
    )

    args = parser.parse_args()
    assert os.path.exists(args.config), f"[** FILEPATH NOT EXISTED **] Can't load config file from {args.config}."

    main(args)