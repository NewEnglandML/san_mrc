import re
import os
import json
import spacy
import unicodedata
import numpy as np
import argparse
import collections
import multiprocessing
import logging
import random
import tqdm
import pickle
from functools import partial
from collections import Counter
## clean up
from my_utils.tokenizer import Vocabulary, reform_text
from my_utils.word2vec_utils import load_glove_vocab, build_embedding
from my_utils.utils import set_environment
from my_utils.log_wrapper import create_logger
from config import set_args

"""
This script is to preproces SQuAD dataset.
TODO: adding multi-thread ...
"""
NLP = spacy.load('en', disable=['vectors', 'textcat', 'parser'])

def build_vocab(data, glove_vocab=None, sort_all=False, thread=24, clean_on=False):
    print('Collect vocab/pos counter/ner counter')
    # docs
    docs = [reform_text(sample['context']) for sample in data]
    doc_tokened = [doc for doc in NLP.pipe(docs, batch_size=64, n_threads=thread)]
    print('Done with doc tokenize')
    questions = [reform_text(sample['question']) for sample in data]
    questions_tokened = [question for question in NLP.pipe(questions, batch_size=64, n_threads=thread)]
    print('Done with question tokenize')

    tag_counter = Counter()
    ner_counter = Counter()
    if sort_all:
        counter = Counter()
        merged = doc_tokened + questions_tokened
        for tokened in tqdm.tqdm(merged, total=len(data)):
            counter.update([w.text for w in tokened if len(w.text) > 0])
            tag_counter.update([w.tag_ for w in tokened if len(w.text) > 0])
            ner_counter.update(['{}_{}'.format(w.ent_type_, w.ent_iob_) for w in tokened])
        vocab = sorted([w for w in counter if w in glove_vocab], key=counter.get, reverse=True)
    else:
        query_counter = Counter()
        doc_counter = Counter()

        for tokened in tqdm.tqdm(doc_tokened, total=len(doc_tokened)):
            doc_counter.update([w.text for w in tokened if len(w.text) > 0])
            tag_counter.update([w.tag_ for w in tokened if len(w.text) > 0])
            ner_counter.update(['{}_{}'.format(w.ent_type_, w.ent_iob_) for w in tokened])

        for tokened in tqdm.tqdm(questions_tokened, total=len(questions_tokened)):
            query_counter.update([w.text for w in tokened if len(w.text) > 0])
            tag_counter.update([w.tag_ for w in tokened if len(w.text) > 0])
            ner_counter.update(['{}_{}'.format(w.ent_type_, w.ent_iob_) for w in tokened])
        counter = query_counter + doc_counter
        # sort query words
        vocab = sorted([w for w in query_counter if w in glove_vocab], key=query_counter.get, reverse=True)
        vocab += sorted([w for w in doc_counter.keys() - query_counter.keys() if w in glove_vocab], key=counter.get, reverse=True)
    tag_counter = sorted([w for w in tag_counter], key=tag_counter.get, reverse=True)
    ner_counter = sorted([w for w in ner_counter], key=ner_counter.get, reverse=True)

    total = sum(counter.values())
    matched = sum(counter[w] for w in vocab)
    print('Raw vocab size vs vocab in glove: {0}/{1}'.format(len(counter), len(vocab)))
    print('OOV rate:{0:.4f}={1}/{2}'.format(100.0 * (total - matched)/total, (total - matched), total))
    vocab = Vocabulary.build(vocab)
    tag_vocab = Vocabulary.build(tag_counter)
    ner_vocab = Vocabulary.build(ner_counter)
    print('final vocab size: {}'.format(len(vocab)))
    print('POS Tag vocab size: {}'.format(len(tag_vocab)))
    print('NER Tag vocab size: {}'.format(len(ner_vocab)))

    return vocab, tag_vocab, ner_vocab


def load_data(path, is_train=True):
    rows = []
    with open(path, encoding="utf8") as f:
        data = json.load(f)['data']
    # parse data
    for article in tqdm.tqdm(data, total=len(data)):
        for paragraph in article['paragraphs']:
            context = paragraph['context']
            for qa in paragraph['qas']:
                uid, question = qa['id'], qa['question']
                answers = qa.get('answers', [])
                if is_train:
                    if len(answers) < 1: continue
                    answer = answers[0]['text']
                    answer_start = answers[0]['answer_start']
                    answer_end = answer_start + len(answer)
                    sample = {'uid': uid, 'context': context, 'question': question, 'answer': answer, 'answer_start': answer_start, 'answer_end':answer_end}
                else:
                    sample = {'uid': uid, 'context': context, 'question': question, 'answer': answers, 'answer_start': -1, 'answer_end':-1}
                rows.append(sample)
    return rows

def postag_func(toks, vocab):
    return [vocab[w.tag_] for w in toks if len(w.text) > 0]

def nertag_func(toks, vocab):
    return [vocab['{}_{}'.format(w.ent_type_, w.ent_iob_)] for w in toks if len(w.text) > 0]

def tok_func(toks, vocab):
    return [vocab[w.text] for w in toks if len(w.text) > 0]

def match_func(question, context):
    counter = Counter(w.text.lower() for w in context)
    total = sum(counter.values())
    freq = [counter[w.text.lower()] / total for w in context]
    question_word = {w.text for w in question}
    question_lower = {w.text.lower() for w in question}
    question_lemma = {w.lemma_ if w.lemma_ != '-PRON-' else w.text.lower() for w in question}
    match_origin = [1 if w in question_word else 0 for w in context]
    match_lower = [1 if w.text.lower() in question_lower else 0 for w in context]
    match_lemma = [1 if (w.lemma_ if w.lemma_ != '-PRON-' else w.text.lower()) in question_lemma else 0 for w in context]
    features = np.asarray([freq, match_origin, match_lower, match_lemma], dtype=np.float32).T.tolist()
    return features

def build_span(context, answer, context_token, answer_start, answer_end, is_train=True):
    p_str = 0
    p_token = 0
    t_start, t_end, t_span = -1, -1, []
    while p_str < len(context):
        if re.match('\s', context[p_str]):
            p_str += 1
            continue
        token = context_token[p_token]
        token_len = len(token)
        if context[p_str:p_str + token_len] != token:
            return (None, None, [])
        t_span.append((p_str, p_str + token_len))
        if is_train:
            if (p_str <= answer_start and answer_start < p_str + token_len):
                t_start = p_token
            if (p_str < answer_end and answer_end <= p_str + token_len):
                t_end = p_token
        p_str += token_len
        p_token += 1
    if is_train and (t_start == -1 or t_end == -1):
        return (-1, -1, [])
    else:
        return (t_start, t_end, t_span)

def feature_func(sample, vocab, vocab_tag, vocab_ner, is_train=True):
    query_tokend = NLP(reform_text(sample['question']))
    doc_tokend = NLP(reform_text(sample['context']))
    # features
    fea_dict = {}
    fea_dict['uid'] = sample['uid']
    fea_dict['context'] = sample['context']
    fea_dict['query_tok'] = tok_func(query_tokend, vocab)
    fea_dict['query_pos'] = postag_func(query_tokend, vocab_tag)
    fea_dict['query_ner'] = nertag_func(query_tokend, vocab_ner)
    fea_dict['doc_tok'] = tok_func(doc_tokend, vocab)
    fea_dict['doc_pos'] = postag_func(doc_tokend, vocab_tag)
    fea_dict['doc_ner'] = nertag_func(doc_tokend, vocab_ner)
    fea_dict['doc_fea'] = '{}'.format(match_func(query_tokend, doc_tokend)) # json don't support float
    doc_toks = [t.text for t in doc_tokend]
    start, end, span = build_span(sample['context'], sample['answer'], doc_toks, sample['answer_start'], sample['answer_end'], is_train=is_train)
    if is_train and (start == -1 or end == -1): return None
    fea_dict['span'] = span
    fea_dict['start'] = start
    fea_dict['end'] = end
    return fea_dict

def build_data(data, vocab, vocab_tag, vocab_ner, fout, is_train):
    with open(fout, 'w', encoding='utf-8') as writer:
        dropped_sample = 0
        for sample in tqdm.tqdm(data, total=len(data)):
            fd = feature_func(sample, vocab, vocab_tag, vocab_ner, is_train)
            if fd is None:
                dropped_sample += 1
                continue
            writer.write('{}\n'.format(json.dumps(fd)))
        logger.info('dropped {} in total {}'.format(dropped_sample, len(data)))

def main():
    args = set_args()
    global logger
    logger = create_logger(__name__, to_disk=True, log_file=args.log_file)
    logger.info('~Processing SQuAD dataset~')
    train_path = os.path.join(args.data_dir, 'train-v1.1.json')
    valid_path = os.path.join(args.data_dir, 'dev-v1.1.json')
    logger.info('The path of training data: {}'.format(train_path))
    logger.info('The path of validation data: {}'.format(valid_path))
    logger.info('{}-dim word vector path: {}'.format(args.glove_dim, args.glove))
    glove_path = args.glove
    glove_dim = args.glove_dim
    set_environment(args.seed)
    logger.info('Loading glove vocab.')
    glove_vocab = load_glove_vocab(glove_path, glove_dim)
    # load data
    train_data = load_data(train_path)
    valid_data = load_data(valid_path, False)

    logger.info('Build vocabulary')
    vocab, vocab_tag, vocab_ner = build_vocab(train_data + valid_data, glove_vocab, sort_all=args.sort_all, clean_on=True)
    meta_path = os.path.join(args.data_dir, args.meta)
    logger.info('building embedding')
    embedding = build_embedding(glove_path, vocab, glove_dim)
    meta = {'vocab': vocab, 'vocab_tag': vocab_tag, 'vocab_ner': vocab_ner, 'embedding': embedding}
    with open(meta_path, 'wb') as f:
        pickle.dump(meta, f)

    train_fout = os.path.join(args.data_dir, args.train_data)
    build_data(train_data, vocab, vocab_tag, vocab_ner, train_fout, True)
    dev_fout = os.path.join(args.data_dir, args.dev_data)
    build_data(valid_data, vocab, vocab_tag, vocab_ner, dev_fout, False)

if __name__ == '__main__':
    main()
