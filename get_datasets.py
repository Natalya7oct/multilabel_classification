

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

def write_formated_data_coco(args, captions, instances):

  data = pd.DataFrame(captions['images'])
  captions = pd.DataFrame(captions['annotations'])
  captions = captions.groupby('image_id')['caption'].apply(tuple)
  data['caption'] = data['id'].map(captions)

  cat_dict = {x['id']: x['name'] for x in instances['categories']}

  def category_agg(cats):
      cats = set(cats)
      cats = list(cats)
      cats = sorted(cats)
      cats = tuple([cat_dict[cat] for cat in cats])
      return cats

  tags = pd.DataFrame(instances['annotations'])
  tags = tags.groupby('image_id')['category_id'].apply(category_agg)

  data['tags'] = data['id'].map(tags)

  data = data[~data['tags'].isna()]

  data = data[['id', 'coco_url', 'caption', 'tags']]
  data = data.sort_values('id')

  data = data.reset_index(drop=True)

  lines = []
  for i in tqdm(range(len(data))):
    try:
      img = requests.get(data.coco_url[i]).content
      with open(f'''{args.data_path_coco}/dataset/''' + f'''{data['coco_url'][i][38:]}''', 'wb') as handler:
        handler.write(img)
      lines.append({'label': list(data.tags[i]), 'img': f'''dataset/{data['coco_url'][i][38:]}''', 'text': data.caption[i][0]})
    except:
      pass

  lines_train = lines[:int(len(lines)*args.train_perc)]
  lines_val = lines[int(len(lines)*args.train_perc):int(len(lines)*args.train_perc)+int(len(lines)*args.val_perc)]
  lines_test = lines[int(len(lines)*args.train_perc)+int(len(lines)*args.val_perc):]

  with open(f'{args.data_path_coco}/train.jsonl', 'w') as f:
      for item in lines_train:
          f.write(json.dumps(item) + "\n")

  with open(f'{args.data_path_coco}/val.jsonl', 'w') as f:
      for item in lines_val:
          f.write(json.dumps(item) + "\n")

  with open(f'{args.data_path_coco}/test.jsonl', 'w') as f:
      for item in lines_test:
          f.write(json.dumps(item) + "\n")

def write_formated_data_mmimdb(args):

  id_list = []
  for file in sorted(os.listdir(f'{args.data_path_mmimdb}/dataset')):
    id_list.append(file.split('.')[0])
  id_list = list(set(id_list))
  len_id_list = len(id_list)

  id_list_train = id_list[:int(len_id_list*args.train_perc)]
  id_list_val = id_list[int(len_id_list*args.train_perc):int(len_id_list*args.train_perc)+int(len_id_list*args.val_perc)]
  id_list_test = id_list[int(len_id_list*args.train_perc)+int(len_id_list*args.val_perc):]

  train_labels = set()
  train = []
  for idx in id_list_train:
      data = json.load(open(f'{args.data_path_mmimdb}/dataset/{idx}.json'))
      train.append(
          {
              'label': data['genres'],
              'img': f'dataset/{idx}.jpeg',
              'text': data['plot'][0]
          }
      )
      for label in data['genres']:
          train_labels.add(label)
  with jsonlines.open(f'{args.data_path_mmimdb}/train.jsonl', 'w') as writer:
    writer.write_all(train)


  val = []
  for idx in id_list_val:
      data = json.load(open(f'{args.data_path_mmimdb}/dataset/{idx}.json'))
      label = [label for label in data['genres'] if label in train_labels]
      if len(label)>0:
          val.append(
              {
                  'label': label,
                  'img': f'dataset/{idx}.jpeg',
                  'text': data['plot'][0]
              }
          )
  with jsonlines.open(f'{args.data_path_mmimdb}/val.jsonl', 'w') as writer:
    writer.write_all(val)

  test = []
  for idx in id_list_test:
      data = json.load(open(f'{args.data_path_mmimdb}/dataset/{idx}.json'))
      label = [label for label in data['genres'] if label in train_labels]
      if len(label)>0:
          test.append(
              {
                  'label': label,
                  'img': f'dataset/{idx}.jpeg',
                  'text': data['plot'][0]
              }
          )
  with jsonlines.open(f'{args.data_path_mmimdb}/test.jsonl', 'w') as writer:
    writer.write_all(test)
