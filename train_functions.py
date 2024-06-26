
import json
import jsonlines
import numpy as np
import os
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
import functools
from collections import Counter
from argparse import Namespace
from transformers import BertTokenizer, BertModel
from tqdm.notebook import tqdm
import re
import pandas as pd
import torch
import torchvision
import torch.nn as nn
import torch.optim as optim
import torchvision.transforms as transforms
from torch.utils.data import Dataset
from torch.utils.data import DataLoader

import shutil
from sklearn.metrics import f1_score
from sklearn.metrics import precision_recall_curve
import matplotlib.pyplot as plt
import requests

class Vocab(object):
    def __init__(self, emptyInit=False):
        if emptyInit:
            self.stoi, self.itos, self.vocab_sz = {}, [], 0
        else:
            self.stoi = {
                w: i
                for i, w in enumerate(['[PAD]', '[UNK]', '[CLS]', '[SEP]', '[MASK]'])
            }
            self.itos = [w for w in self.stoi]
            self.vocab_sz = len(self.itos)

    def add(self, words):
        cnt = len(self.itos)
        for w in words:
            if w in self.stoi:
                continue
            self.stoi[w] = cnt
            self.itos.append(w)
            cnt += 1
        self.vocab_sz = len(self.itos)

model_transforms = transforms.Compose(
    [
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(
            mean=[0.46777044, 0.44531429, 0.40661017],
            std=[0.12221994, 0.12145835, 0.14380469],
        ),
    ]
)

class JsonlDataset(Dataset):
    def __init__(self, data_path, tokenizer, transforms, vocab, args):
        self.data = [json.loads(l) for l in open(data_path)]
        self.data_dir = os.path.dirname(data_path)
        self.tokenizer = tokenizer
        self.args = args
        self.vocab = vocab
        self.n_classes = len(args.labels)
        self.text_start_token = ['[CLS]']

        self.max_seq_len = args.max_seq_len
        self.transforms = transforms

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        sentence = (
            self.text_start_token
            + self.tokenizer(self.data[index]['text'])[:(self.args.max_seq_len - 1)]
        )
        segment = torch.zeros(len(sentence))

        sentence = torch.LongTensor(
            [
                self.vocab.stoi[w] if w in self.vocab.stoi else self.vocab.stoi['[UNK]']
                for w in sentence
            ]
        )

        label = torch.zeros(self.n_classes)
        label[
            [self.args.labels.index(tgt) for tgt in self.data[index]['label']]
        ] = 1

        if self.data[index]['img']:
            image = Image.open(os.path.join(self.data_dir, self.data[index]['img'])).convert('RGB')

        image = self.transforms(image)

        return sentence, segment, image, label

def collate_fn(batch, args):
    lens = [len(row[0]) for row in batch]
    bsz, max_seq_len = len(batch), max(lens)

    mask_tensor = torch.zeros(bsz, max_seq_len).long()
    text_tensor = torch.zeros(bsz, max_seq_len).long()
    segment_tensor = torch.zeros(bsz, max_seq_len).long()

    img_tensor = torch.stack([row[2] for row in batch])

    tgt_tensor = torch.stack([row[3] for row in batch])

    for i_batch, (input_row, length) in enumerate(zip(batch, lens)):
        tokens, segment = input_row[:2]
        text_tensor[i_batch, :length] = tokens
        segment_tensor[i_batch, :length] = segment
        mask_tensor[i_batch, :length] = 1

    return text_tensor, segment_tensor, mask_tensor, img_tensor, tgt_tensor

def get_dataloader(data_path, args):

  label_freqs = Counter()
  data_labels = [json.loads(line)['label'] for line in open(os.path.join(data_path, 'train.jsonl'))]

  if type(data_labels[0]) == list:
      for label_row in data_labels:
          label_freqs.update(label_row)
  else:
      label_freqs.update(data_labels)

  args.labels = list(label_freqs.keys())
  args.label_freqs = label_freqs

  tokenizer = BertTokenizer.from_pretrained(args.bert_type, do_lower_case=True)
  vocab = Vocab()
  vocab.stoi = tokenizer.vocab
  vocab.itos = tokenizer.ids_to_tokens
  vocab.vocab_sz = len(vocab.itos)
  args.vocab = vocab
  args.vocab_sz = vocab.vocab_sz
  args.n_classes = len(args.labels)
  tokenizer = tokenizer.tokenize

  model_transforms = transforms.Compose(
      [
          transforms.Resize(256),
          transforms.CenterCrop(224),
          transforms.ToTensor(),
          transforms.Normalize(
              mean=[0.46777044, 0.44531429, 0.40661017],
              std=[0.12221994, 0.12145835, 0.14380469],
          ),
      ]
  )

  collate = functools.partial(collate_fn, args=args)

  train = JsonlDataset(os.path.join(data_path, 'train.jsonl'),tokenizer,model_transforms,vocab,args,)
  train_loader = DataLoader(train,batch_size=args.batch_sz,shuffle=True,num_workers=args.n_workers,collate_fn=collate,drop_last=True,)
  args.train_data_len = len(train)

  val = JsonlDataset(os.path.join(data_path, 'val.jsonl'),tokenizer,model_transforms,vocab,args,)
  val_loader = DataLoader(val,batch_size=args.batch_sz,shuffle=False,num_workers=args.n_workers,collate_fn=collate,)

  test = JsonlDataset(os.path.join(data_path, 'test.jsonl'),tokenizer,model_transforms,vocab,args,)
  test_loader = DataLoader(test,batch_size=args.batch_sz,shuffle=False,num_workers=args.n_workers,collate_fn=collate,)

  return train_loader, val_loader, test_loader, args

def find_threshold_f1(trues, logits, eps=1e-9):
    precision, recall, thresholds = precision_recall_curve(trues, logits)
    f1_scores = 2 * precision * recall / (precision + recall + eps)
    threshold = float(thresholds[np.argmax(f1_scores)])
    return np.max(f1_scores), threshold

def save_checkpoint(state, is_best, checkpoint_path, filename='checkpoint.pt'):
    filename = os.path.join(checkpoint_path, filename)
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, os.path.join(checkpoint_path, 'model_best.pt'))

def load_checkpoint(model, path):
    best_checkpoint = torch.load(path)
    model.load_state_dict(best_checkpoint['state_dict'])

def model_eval(i_epoch, data, model, args):
    with torch.no_grad():
        losses, preds, tgts = [], [], []
        for batch in data:
            txt, segment, mask, img, tgt = batch

            txt, img = txt.cuda(), img.cuda()
            mask, segment = mask.cuda(), segment.cuda()
            tgt = tgt.cuda()

            out = model(txt, mask, segment, img)
            loss = args.criterion(out, tgt)
            losses.append(loss.item())

            pred = torch.sigmoid(out).cpu().detach().numpy()

            preds.append(pred.tolist())
            tgt = tgt.cpu().detach().numpy().tolist()
            tgts.extend(tgt)

    metrics = {'loss': np.mean(losses)}
    tgts = np.vstack(tgts)
    preds = np.vstack(preds)

    f1_scores_list = []
    tresholds_list = []

    for i in range(tgts.shape[1]):
      f1, treshold = find_threshold_f1(tgts[:,i], preds[:,i], eps=1e-9)
      f1_scores_list.append(f1)
      tresholds_list.append(treshold)

    metrics['macro_f1'] = sum(f1_scores_list) / len(f1_scores_list)

    return metrics

def model_train(model, args, savedir):

  optimizer = args.optimizer
  scheduler = args.scheduler
  criterion = args.criterion

  start_epoch, global_step, n_no_improve, best_metric = 0, 0, 0, -np.inf

  epoch_train_losses = []
  epoch_val_losses = []

  for i_epoch in range(start_epoch, args.max_epochs):
      train_losses = []
      model.train()
      optimizer.zero_grad()

      for batch in tqdm(args.train_loader, total=len(args.train_loader)):
          txt, segment, mask, img, tgt = batch

          txt, img = txt.cuda(), img.cuda()
          mask, segment = mask.cuda(), segment.cuda()
          tgt = tgt.cuda()
          out = model(txt, mask, segment, img)
          loss = criterion(out, tgt)

          train_losses.append(loss.item())
          loss.backward()
          global_step += 1
          if global_step % args.gradient_accumulation_steps == 0:
              optimizer.step()
              optimizer.zero_grad()

      model.eval()
      metrics = model_eval(i_epoch, args.val_loader, model, args)
      epoch_train_losses.append(np.mean(train_losses))
      epoch_val_losses.append(metrics['loss'])
      print('Epoch:', i_epoch)
      print('Train Loss: {:.4f}'.format(np.mean(train_losses)))
      print('{}: Loss: {:.5f} | Macro F1 {:.5f} '.format('Val', metrics['loss'], metrics['macro_f1']))

      if len(epoch_train_losses) > 5:
          plt.figure(figsize=(30, 5))
          plt.plot(epoch_train_losses)
          plt.plot(epoch_val_losses)
          plt.grid()
          plt.show()

      tuning_metric = metrics['macro_f1']
      scheduler.step(tuning_metric)
      is_improvement = tuning_metric > best_metric
      if is_improvement:
          best_metric = tuning_metric
          n_no_improve = 0
      else:
          n_no_improve += 1

      save_checkpoint(
          {'epoch': i_epoch + 1, 'state_dict': model.state_dict(),'optimizer': optimizer.state_dict(),'scheduler': scheduler.state_dict(),'n_no_improve': n_no_improve,'best_metric': best_metric,},
          is_improvement,
          savedir,
      )

      if n_no_improve >= args.patience:
          print('No improvement. Breaking out of loop.')
          break

def main(args, dataset_path):
  args.train_loader, args.val_loader, args.test_loader, args = get_dataloader(dataset_path, args)
  freqs = [args.label_freqs[l] for l in args.labels]
  label_weights = (torch.FloatTensor(freqs) / args.train_data_len) ** -1
  args.criterion = nn.BCEWithLogitsLoss(pos_weight=label_weights.cuda())

  bert_model = BertModel.from_pretrained(args.bert_type)
  if args.resnet_type == 'resnet152':
    resnet_model = torchvision.models.resnet152(pretrained=True)
  elif args.resnet_type == 'resnet50':
    resnet_model = torchvision.models.resnet50(pretrained=True)
  elif args.resnet_type == 'resnet18':
    resnet_model = torchvision.models.resnet18(pretrained=True)
  else:
    print('Unknown model')


  class BertEncoder(nn.Module):
      def __init__(self, args):
          super(BertEncoder, self).__init__()
          self.args = args
          self.bert = bert_model

      def forward(self, txt, mask, segment):
          out = self.bert(
              txt,
              token_type_ids=segment,
              attention_mask=mask,
              output_hidden_states=False,
          )
          return out.pooler_output

  class ImageEncoder(nn.Module):
      def __init__(self, args):
          super(ImageEncoder, self).__init__()
          self.args = args
          model = resnet_model
          modules = list(model.children())[:-2]
          self.model = nn.Sequential(*modules)

          pool_func = (
              nn.AdaptiveAvgPool2d
              if args.img_embed_pool_type == 'avg'
              else nn.AdaptiveMaxPool2d
          )

          if args.num_image_embeds in [1, 2, 3, 5, 7]:
              self.pool = pool_func((args.num_image_embeds, 1))
          elif args.num_image_embeds == 4:
              self.pool = pool_func((2, 2))
          elif args.num_image_embeds == 6:
              self.pool = pool_func((3, 2))
          elif args.num_image_embeds == 8:
              self.pool = pool_func((4, 2))
          elif args.num_image_embeds == 9:
              self.pool = pool_func((3, 3))

      def forward(self, x):
          out = self.pool(self.model(x))
          out = torch.flatten(out, start_dim=2)
          out = out.transpose(1, 2).contiguous()
          return out

  class MultimodalModel(nn.Module):
      def __init__(self, args):
          super(MultimodalModel, self).__init__()
          self.args = args
          self.txtenc = BertEncoder(args)
          self.imgenc = ImageEncoder(args)

          last_size = args.text_hidden_sz + (args.img_hidden_sz * args.num_image_embeds)
          self.clf = nn.ModuleList()

          self.clf.append(nn.Linear(last_size, args.linear_layer_dim))
          for i in range(args.linear_layer_count):
            self.clf.append(nn.Linear(args.linear_layer_dim, args.linear_layer_dim))
          self.clf.append(nn.Linear(args.linear_layer_dim, args.n_classes))

      def forward(self, txt, mask, segment, img):
          txt = self.txtenc(txt, mask, segment)
          img = self.imgenc(img)
          img = torch.flatten(img, start_dim=1)
          out = torch.cat([txt, img], -1)
          for layer in self.clf:
              out = layer(out)
          return out

  class TextModel(nn.Module):
      def __init__(self, args):
          super(TextModel, self).__init__()
          self.args = args
          self.txtenc = BertEncoder(args)
          self.imgenc = ImageEncoder(args)

          last_size = args.text_hidden_sz
          self.clf = nn.ModuleList()

          self.clf.append(nn.Linear(last_size, args.linear_layer_dim))
          for i in range(args.linear_layer_count):
            self.clf.append(nn.Linear(args.linear_layer_dim, args.linear_layer_dim))
          self.clf.append(nn.Linear(args.linear_layer_dim, args.n_classes))

      def forward(self, txt, mask, segment, img):
          txt = self.txtenc(txt, mask, segment)
          img = self.imgenc(img)
          img = torch.flatten(img, start_dim=1)
          out = txt
          for layer in self.clf:
              out = layer(out)
          return out

  class ImgModel(nn.Module):
      def __init__(self, args):
          super(ImgModel, self).__init__()
          self.args = args
          self.txtenc = BertEncoder(args)
          self.imgenc = ImageEncoder(args)

          last_size = args.img_hidden_sz * args.num_image_embeds
          self.clf = nn.ModuleList()

          self.clf.append(nn.Linear(last_size, args.linear_layer_dim))
          for i in range(args.linear_layer_count):
            self.clf.append(nn.Linear(args.linear_layer_dim, args.linear_layer_dim))
          self.clf.append(nn.Linear(args.linear_layer_dim, args.n_classes))

      def forward(self, txt, mask, segment, img):
          txt = self.txtenc(txt, mask, segment)
          img = self.imgenc(img)
          img = torch.flatten(img, start_dim=1)
          out = img
          for layer in self.clf:
              out = layer(out)
          return out

  class MultimodalModelAvg(nn.Module):
      def __init__(self, args):
          super(MultimodalModelAvg, self).__init__()
          self.args = args
          self.txtmodel = TextModel(args).cuda()
          self.imgmodel = ImgModel(args).cuda()

      def forward(self, txt, mask, segment, img):
          txt_i, mask_i, segment_i, img_i = txt, mask, segment, img
          txt_t, mask_t, segment_t, img_t = txt, mask, segment, img
          img = self.imgmodel(txt_i, mask_i, segment_i, img_i)
          txt = self.txtmodel(txt_t, mask_t, segment_t, img_t)
          out = (txt+img)/2
          return out


  model_type = []
  params_count = []
  test_f1 = []

  
  print('MultimodelAvg model')
  model = MultimodalModelAvg(args).cuda()

  model_parameters = filter(lambda p: p.requires_grad, model.parameters())
  params = sum([np.prod(p.size()) for p in model_parameters])
  print('Number of parameters: {:.5f} '.format(params))

  args.optimizer = optim.AdamW(model.parameters(), lr=args.lr)
  args.scheduler = optim.lr_scheduler.ReduceLROnPlateau(args.optimizer, 'max', patience=args.lr_patience, verbose=True, factor=args.lr_factor)
  torch.save(args, os.path.join(args.savedir_multimodal, 'args.pt'))
  model_train(model, args, args.savedir_multimodal)
  load_checkpoint(model, os.path.join(args.savedir_multimodal, 'model_best.pt'))
  model.eval()
  test_metrics = model_eval(np.inf, args.test_loader, model, args)
  print('{}: Loss: {:.5f} | Macro F1 {:.5f}'.format('Test', test_metrics['loss'], test_metrics['macro_f1']))
  model_type.append('multimodel_avg')
  params_count.append(params)
  test_f1.append(test_metrics['macro_f1'])
  

  print('Multimodel model')
  model = MultimodalModel(args).cuda()

  model_parameters = filter(lambda p: p.requires_grad, model.parameters())
  params = sum([np.prod(p.size()) for p in model_parameters])
  print('Number of parameters: {:.5f} '.format(params))

  args.optimizer = optim.AdamW(model.parameters(), lr=args.lr)
  args.scheduler = optim.lr_scheduler.ReduceLROnPlateau(args.optimizer, 'max', patience=args.lr_patience, verbose=True, factor=args.lr_factor)
  torch.save(args, os.path.join(args.savedir_multimodal, 'args.pt'))
  model_train(model, args, args.savedir_multimodal)
  load_checkpoint(model, os.path.join(args.savedir_multimodal, 'model_best.pt'))
  model.eval()
  test_metrics = model_eval(np.inf, args.test_loader, model, args)
  print('{}: Loss: {:.5f} | Macro F1 {:.5f}'.format('Test', test_metrics['loss'], test_metrics['macro_f1']))
  model_type.append('multimodel')
  params_count.append(params)
  test_f1.append(test_metrics['macro_f1'])


  print('Text model')
  model = TextModel(args).cuda()

  model_parameters = filter(lambda p: p.requires_grad, model.parameters())
  params = sum([np.prod(p.size()) for p in model_parameters])
  print('Number of parameters: {:.5f} '.format(params))

  optimizer = optim.AdamW(model.parameters(), lr=args.lr)
  scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=args.lr_patience, verbose=True, factor=args.lr_factor)
  torch.save(args, os.path.join(args.savedir_text, 'args.pt'))
  model_train(model, args, args.savedir_text)
  load_checkpoint(model, os.path.join(args.savedir_text, 'model_best.pt'))
  model.eval()
  test_metrics = model_eval(np.inf, args.test_loader, model, args)
  print('{}: Loss: {:.5f} | Macro F1 {:.5f}'.format('Test', test_metrics['loss'], test_metrics['macro_f1']))
  model_type.append('text')
  params_count.append(params)
  test_f1.append(test_metrics['macro_f1'])



  print('Image model')
  model = ImgModel(args).cuda()

  model_parameters = filter(lambda p: p.requires_grad, model.parameters())
  params = sum([np.prod(p.size()) for p in model_parameters])
  print('Number of parameters: {:.5f} '.format(params))

  optimizer = optim.AdamW(model.parameters(), lr=args.lr)
  scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=args.lr_patience, verbose=True, factor=args.lr_factor)
  torch.save(args, os.path.join(args.savedir_image, 'args.pt'))
  model_train(model, args, args.savedir_image)
  load_checkpoint(model, os.path.join(args.savedir_image, 'model_best.pt'))
  model.eval()
  test_metrics = model_eval(np.inf, args.test_loader, model, args)
  print('{}: Loss: {:.5f} | Macro F1 {:.5f}'.format('Test', test_metrics['loss'], test_metrics['macro_f1']))
  model_type.append('image')
  params_count.append(params)
  test_f1.append(test_metrics['macro_f1'])

  return model_type, params_count, test_f1
