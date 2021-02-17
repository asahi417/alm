import re
import os
import logging
import math
from itertools import chain
from typing import List
from tqdm import tqdm
from copy import deepcopy
logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')

import transformers
import torch
from torch import nn

os.environ["TOKENIZERS_PARALLELISM"] = "false"  # to turn off warning message
PAD_TOKEN_LABEL_ID = nn.CrossEntropyLoss().ignore_index


def get_partition(_list):
    length = list(map(lambda x: len(x), _list))
    return list(map(lambda x: [sum(length[:x]), sum(length[:x + 1])], range(len(length))))


class Dataset(torch.utils.data.Dataset):
    """ `torch.utils.data.Dataset` """
    float_tensors = ['attention_mask']

    def __init__(self, data: List):
        self.data = data  # a list of dictionaries

    def __len__(self):
        return len(self.data)

    def to_tensor(self, name, data):
        if name in self.float_tensors:
            return torch.tensor(data, dtype=torch.float32)
        return torch.tensor(data, dtype=torch.long)

    def __getitem__(self, idx):
        return {k: self.to_tensor(k, v) for k, v in self.data[idx].items()}


class Prompter:
    """ transformers language model based sentence-mining """

    def __init__(self,
                 model: str,
                 max_length: int = None,
                 cache_dir: str = './cache',
                 num_worker: int = 0):
        """ transformers language model based sentence-mining

        :param model: a model name corresponding to a model card in `transformers`
        :param max_length: a model max length if specified, else use model_max_length
        """
        logging.debug('*** setting up a language model ***')
        self.num_worker = num_worker
        if self.num_worker == 1:
            os.environ["OMP_NUM_THREADS"] = "1"  # to turn off warning message

        self.model_type = None
        self.model_name = model
        self.cache_dir = cache_dir
        self.device = 'cpu'
        self.model = None
        self.is_causal = 'gpt' in self.model_name  # TODO: fix to be more comprehensive method
        assert not self.is_causal
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(model, cache_dir=cache_dir)
        if self.is_causal:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.config = transformers.AutoConfig.from_pretrained(model, cache_dir=cache_dir)
        if max_length:
            assert self.tokenizer.model_max_length >= max_length, '{} < {}'.format(self.tokenizer.model_max_length,
                                                                                   max_length)
            self.max_length = max_length
        else:
            self.max_length = self.tokenizer.model_max_length

        # sentence prefix tokens
        tokens = self.tokenizer.tokenize('get tokenizer specific prefix')
        tokens_encode = self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode('get tokenizer specific prefix'))
        self.sp_token_prefix = tokens_encode[:tokens_encode.index(tokens[0])]
        self.sp_token_suffix = tokens_encode[tokens_encode.index(tokens[-1]) + 1:]

    def input_ids_to_labels(self, input_ids, label_position: List = None, label_id: List = None):
        """ Generate label for likelihood computation

        :param input_ids: input_ids given by tokenizer.encode
        :param label_position: position to keep for label
        :param label_id: indices to use in `label_position`
        :return: labels, a list of indices for loss computation
        """
        if label_position is None and label_id is None:
            # ignore padding token
            label = list(map(lambda x: PAD_TOKEN_LABEL_ID if x == self.tokenizer.pad_token_id else x, input_ids))
        else:
            assert len(label_position) == len(label_id)
            label = [PAD_TOKEN_LABEL_ID] * len(input_ids)
            for p, i in zip(label_position, label_id):
                label[p] = i
        if self.is_causal:  # shift the label sequence for causal inference
            label = label[1:] + [PAD_TOKEN_LABEL_ID]
        return label

    def cleanup_decode(self, sentence):
        # give a space around <mask>
        cleaned_sent = re.sub(r'({})'.format(self.tokenizer.mask_token).replace('[', '\[').replace(']', '\]'),
                              r' \1 ', sentence)
        cleaned_sent = re.sub(r'\s+', ' ', cleaned_sent)  # reduce more than two space to one

        # remove special tokens but mask
        to_remove = list(filter(lambda x: x != self.tokenizer.mask_token, self.tokenizer.all_special_tokens))
        to_remove = '|'.join(to_remove).replace('[', '\[').replace(']', '\]')
        cleaned_sent = re.sub(r'{}'.format(to_remove), '', cleaned_sent)
        # remove redundant spaces at the prefix
        return re.sub(r'\A\s*', '', cleaned_sent)

    def load_model(self):
        """ Model setup """
        logging.info('load language model')
        params = dict(config=self.config, cache_dir=self.cache_dir)
        if self.is_causal:
            self.model = transformers.AutoModelForCausalLM.from_pretrained(self.model_name, **params)
            self.model_type = 'causal_lm'
        else:
            self.model = transformers.AutoModelForMaskedLM.from_pretrained(self.model_name, **params)
            self.model_type = 'masked_lm'
        self.model.eval()
        # gpu
        n_gpu = torch.cuda.device_count()
        assert n_gpu <= 1
        self.device = 'cuda' if n_gpu > 0 else 'cpu'
        self.model.to(self.device)
        logging.info('running on {} GPU'.format(n_gpu))

    def pair_to_seed(self,
                     word_pair: List,
                     n_blank: int = 3,
                     n_blank_prefix: int = 2,
                     n_blank_suffix: int = 2,
                     batch_size: int = 4,
                     seed_type: str = 'middle'):
        assert len(word_pair) == 2, '{}'.format(len(word_pair))
        h, t = word_pair
        if seed_type == 'middle':
            return ' '.join([h] + [self.tokenizer.mask_token] * n_blank + [t])
        elif seed_type == 'whole':
            return ' '.join([self.tokenizer.mask_token] * n_blank_prefix + [h] + [self.tokenizer.mask_token] * n_blank
                            + [t] + [self.tokenizer.mask_token] * n_blank_suffix)
        elif seed_type == 'best':
            # build candidates
            candidates = []
            for pre_n in range(self.max_length - 2):
                prefix = [self.tokenizer.mask_token] * pre_n + [h]
                for mid_n in range(1, self.max_length - 1 - pre_n):
                    middle = [self.tokenizer.mask_token] * mid_n + [t]
                    candidates.append(' '.join(prefix + middle))
            # compute perplexity
            logging.info('find best seed position for head and tail by perplexity: {} in total'.format(len(candidates)))
            ppl = self.get_perplexity(candidates, batch_size=batch_size)
            best_seed = candidates[ppl.index(min(ppl))]
            # print(candidates)
            # print(ppl)
            # print(best_seed)
            return best_seed
        else:
            raise ValueError('unknown seed type: {}'.format(seed_type))

    def encode_plus(self,
                    sentence,
                    token_wise_mask: bool = False):
        """ Encode with mask flag, that is masked position if sentence has masked token, otherwise is the entire
        sequence except for special tokens.

        :param sentence:
        :param token_wise_mask:
        :return:
        """
        # TODO: add Error if sentence exceed its max length
        param = {'max_length': self.max_length, 'truncation': True, 'padding': 'max_length'}
        if self.is_causal:
            raise NotImplementedError('only available with masked LM')
        if not token_wise_mask:
            assert self.tokenizer.mask_token in sentence, sentence

            encode = self.tokenizer.encode_plus(sentence, **param)
            assert len(encode['input_ids']) < self.max_length, 'exceed max_length'
            # encode['labels'] = list(map(lambda x: int(x == self.tokenizer.mask_token_id), encode['input_ids']))
            return [encode]
        else:
            token_list = self.tokenizer.tokenize(sentence)

            def encode_with_single_mask_id(mask_position: int):
                _token_list = token_list.copy()  # can not be encode outputs because of prefix
                masked_token_id = self.tokenizer.convert_tokens_to_ids(_token_list[mask_position])
                if masked_token_id == self.tokenizer.mask_token_id:
                    return None
                _token_list[mask_position] = self.tokenizer.mask_token
                tmp_string = self.tokenizer.convert_tokens_to_string(_token_list)
                _encode = self.tokenizer.encode_plus(tmp_string, **param)
                assert len(_encode['input_ids']) < self.max_length, 'exceed max_length'
                _encode['labels'] = self.input_ids_to_labels(
                    _encode['input_ids'],
                    label_position=[mask_position + len(self.sp_token_prefix)],
                    label_id=[masked_token_id])
                return _encode

            length = min(self.max_length - len(self.sp_token_prefix), len(token_list))
            return list(filter(None, map(encode_with_single_mask_id, range(length))))

    def replace_mask(self,
                     word_pairs: List,
                     n_blank: int = 2,
                     n_revision: int = 10,
                     topk: int = 5,
                     topk_per_position: int = 15,
                     seed_type: str = 'middle',
                     batch_size: int = 4,
                     perplexity_filter: bool = True,
                     debug: bool = False,
                     n_blank_prefix: int = 2,
                     n_blank_suffix: int = 2):
        if type(word_pairs[0]) is not list:
            word_pairs = [word_pairs]
        shared = {'n_blank': n_blank, 'seed_type': seed_type,
                  'n_blank_prefix': n_blank_prefix, 'n_blank_suffix': n_blank_suffix, 'batch_size': batch_size}
        seed_sentences = list(map(lambda x: self.pair_to_seed(x, **shared), word_pairs))
        shared = {'word_pairs': word_pairs, 'topk': topk, 'topk_per_position': topk_per_position,
                  'debug': debug, 'batch_size': batch_size, 'perplexity_filter': perplexity_filter}
        logging.info('replace masked token')
        edit = [seed_sentences]
        while True:
            seed_sentences = self.replace_single_mask(seed_sentences, **shared)
            if all(self.tokenizer.mask_token not in i for i in seed_sentences):
                break
            edit.append(seed_sentences)
        logging.info('additional revision: {} steps'.format(n_revision))
        # TODO: remove redundant sentence & check if it is masked or not
        for i in range(n_revision):
            seed_sentences = self.replace_single_mask(seed_sentences, **shared)
            edit.append(seed_sentences)

        edit = list(zip(*edit))

        return seed_sentences, edit

    def replace_single_mask(self,
                            seed_sentences,
                            word_pairs,
                            batch_size: int = 4,
                            topk: int = 5,
                            topk_per_position: int = 5,
                            perplexity_filter: bool = True,
                            debug: bool = False):
        assert len(seed_sentences) == len(word_pairs), '{} != {}'.format(len(seed_sentences), len(word_pairs))
        if self.model is None:
            self.load_model()
        if type(seed_sentences) is str:
            seed_sentences = [seed_sentences]

        # sentence without masked token will perform token wise mask
        data = list(map(
            lambda x: self.encode_plus(x, token_wise_mask=self.tokenizer.mask_token not in x), seed_sentences))
        partition = get_partition(data)
        data_loader = torch.utils.data.DataLoader(
            Dataset(list(chain(*data))),
            num_workers=self.num_worker, batch_size=batch_size, shuffle=False, drop_last=False)
        assert len(word_pairs) == len(partition), '{} != {}'.format(len(word_pairs), len(partition))

        logging.info('Inference on masked token')
        total_input = []
        total_val = []  # batch, mask_size, topk
        total_ind = []
        with torch.no_grad():
            for encode in tqdm(data_loader):
                encode = {k: v.to(self.device) for k, v in encode.items()}
                output = self.model(**encode, return_dict=True)
                prediction_scores = output['logits']
                values, indices = prediction_scores.topk(topk_per_position, dim=-1)
                total_input += encode.pop('input_ids').tolist()
                total_val += values.tolist()
                total_ind += indices.tolist()

        def process_single_sentence(partition_n):
            """ single partition with multiple masks or multiple partitions with single mask """
            head, tail = word_pairs[partition_n]
            s, e = partition[partition_n]
            topk_decoded = []
            for i in range(s, e):
                inp, val, ind = total_input[i], total_val[i], total_ind[i]
                filtered = list(filter(lambda x: inp[x[0]] == self.tokenizer.mask_token_id, enumerate(zip(val, ind))))

                def decode_topk(k, replace_pos, ind_, likelihood):
                    inp_ = deepcopy(inp)
                    inp_[replace_pos] = ind_[k]
                    decoded = self.tokenizer.decode(inp_, skip_special_tokens=False)
                    decoded = self.cleanup_decode(decoded)
                    if head in decoded and tail in decoded:
                        return decoded, likelihood[k]
                    return None

                for _replace_pos, (_val, _ind) in filtered:
                    topk_decoded += list(filter(
                        None,
                        map(lambda x: decode_topk(x, _replace_pos, _ind, _val), range(topk_per_position))
                    ))

            topk_decoded = sorted(topk_decoded, key=lambda x: x[1], reverse=True)
            decode = list(map(lambda x: x[0], topk_decoded))[:min(topk, len(topk_decoded))]
            return decode

        greedy_filling = list(map(process_single_sentence, range(len(partition))))
        if perplexity_filter:
            logging.info('ppl filtering')
            best_edit = []
            for sent in greedy_filling:
                ppl = self.get_perplexity(sent)
                print(list(zip(sent, ppl)))
                best_edit.append(sent[ppl.index(min(ppl))])
        else:
            best_edit = list(map(lambda x: x[0], greedy_filling))

        if debug:
            for o, ed in zip(seed_sentences, best_edit):
                logging.info('- original: {}'.format(o))
                logging.info('- edit    : {}'.format(ed))
        return best_edit

    def get_perplexity(self, sentences, batch_size: int = 4):
        """ compute perplexity on each sentence

        :param batch_size:
        :param sentences:
        :return: a list of perplexity
        """
        if self.model is None:
            self.load_model()
        if type(sentences) is str:
            sentences = [sentences]

        data = list(map(lambda x: self.encode_plus(x, token_wise_mask=True), sentences))
        partition = get_partition(data)

        data_loader = torch.utils.data.DataLoader(
            Dataset(list(chain(*data))),
            num_workers=self.num_worker, batch_size=batch_size, shuffle=False, drop_last=False)
        loss_fct = nn.CrossEntropyLoss(reduction='none')
        nll = []
        with torch.no_grad():
            for encode in tqdm(data_loader):
                encode = {k: v.to(self.device) for k, v in encode.items()}
                labels = encode.pop('labels')
                output = self.model(**encode, return_dict=True)
                prediction_scores = output['logits']
                loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), labels.view(-1))
                loss = loss.view(len(prediction_scores), -1)
                loss = torch.sum(loss, -1)
                nll += list(map(
                    lambda x: x[0] / sum(map(lambda y: y != PAD_TOKEN_LABEL_ID, x[1])),
                    zip(loss.cpu().tolist(), labels.cpu().tolist())
                ))
        perplexity = list(map(lambda x: math.exp(sum(nll[x[0]:x[1]]) / (x[1] - x[0])), partition))
        return perplexity


if __name__ == '__main__':
    # lm = Prompter('albert-base-v1', max_length=12)
    lm = Prompter('roberta-base', max_length=24)
    # stem = ["beauty", "aesthete"]
    candidates_ = [["pleasure", "hedonist"],
                   ["emotion", "demagogue"],
                   ["opinion", "sympathizer"]]
    o_, e_ = lm.replace_mask(
        candidates_,
        batch_size=1,
        seed_type='whole',
        perplexity_filter=True,
        topk=5,
        n_blank=3,
        n_revision=3,
        debug=True)
    print(o_)
    print(e_)
