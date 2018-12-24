import argparse
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

import qiqc
from qiqc.builder import build_preprocessor, build_tokenizer
from qiqc.datasets import load_qiqc
from qiqc.model_selection import classification_metrics, ClassificationResult
from qiqc.utils import pad_sequence, set_seed


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--modeldir', '-m', type=Path, required=True)
    parser.add_argument('--device', '-g', type=int)
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--epochs', '-e', type=int, default=5)
    parser.add_argument('--outdir', '-o', type=str, default='test')
    parser.add_argument('--batchsize', '-b', type=int, default=512)
    parser.add_argument('--optuna-trials', type=int)
    parser.add_argument('--gridsearch', action='store_true')
    parser.add_argument('--cv', type=int, default=5)
    parser.add_argument('--cv-part', type=int)

    args = parser.parse_args(args)
    config = qiqc.config.build_config(args)
    outdir = Path('results') / '/'.join(args.modeldir.parts[1:])
    config['outdir'] = outdir / config['outdir']
    if args.test:
        config['n_rows'] = 300
        config['batchsize'] = 16
        config['epochs'] = 2
    else:
        config['n_rows'] = None
    qiqc.utils.rmtree_after_confirmation(config['outdir'], args.test)

    if args.gridsearch:
        qiqc.model_selection.train_gridsearch(config, train)
    elif args.optuna_trials is not None:
        qiqc.model_selection.train_optuna(config, train)
    else:
        train(config)


def train(config):
    modelconf = qiqc.loader.load_module(config['modeldir'] / 'model.py')
    build_embedding = modelconf.build_embedding
    build_model = modelconf.build_model
    build_sampler = modelconf.build_sampler
    build_optimizer = modelconf.build_optimizer

    print(config)
    set_seed(config['seed'])
    train_df, submit_df = load_qiqc(n_rows=config['n_rows'])
    preprocessor = build_preprocessor(config['preprocessors'])
    tokenizer = build_tokenizer(config['tokenizer'])

    print('Preprocess texts...')
    train_df['tokens'] = train_df.question_text.apply(
        lambda x: tokenizer(preprocessor(x)))
    submit_df['tokens'] = submit_df.question_text.apply(
        lambda x: tokenizer(preprocessor(x)))
    tokens = train_df.tokens.append(submit_df.tokens).values

    print('Build embedding...')
    token2id, embedding = build_embedding(config, tokens)
    train_df['token_ids'] = train_df.tokens.apply(
        lambda xs: pad_sequence([token2id[x] for x in xs], config['maxlen']))
    submit_df['token_ids'] = submit_df.tokens.apply(
        lambda xs: pad_sequence([token2id[x] for x in xs], config['maxlen']))

    # Train : Test split for holdout training
    train_df, test_df = sklearn.model_selection.train_test_split(
        train_df, test_size=0.1, random_state=0)
    train_df.reset_index(drop=True, inplace=True)
    test_df.reset_index(drop=True, inplace=True)

    train_X = torch.Tensor(train_df.token_ids.tolist()).type(torch.long)
    train_W = torch.Tensor(train_df.weights).type(torch.float)
    train_t = torch.Tensor(train_df.target[:, None]).type(torch.float)

    # Prepare submit dataset
    submit_X = torch.Tensor(submit_df.token_ids).type(torch.long)
    submit_X = submit_X.to(config['device'])
    submit_iter = DataLoader(
        submit_X, batch_size=config['batchsize_valid'])

    # Prepare testset
    test_X = torch.Tensor(test_df.token_ids.tolist()).type(torch.long)
    test_X = test_X.to(config['device'])
    test_t = test_df.target[:, None]
    test_iter = DataLoader(
        test_X, batch_size=config['batchsize_valid'])

    splitter = sklearn.model_selection.StratifiedKFold(
        n_splits=config['cv'], shuffle=True, random_state=config['seed'])
    train_results, valid_results = [], []
    best_models, submit_ys, test_ys = {}, {}, {}
    for i_cv, (train_indices, valid_indices) in enumerate(
            splitter.split(train_X, train_t)):
        if config['cv_part'] is not None and i_cv >= config['cv_part']:
            break
        _train_X = train_X[train_indices].to(config['device'])
        _train_W = train_W[train_indices].to(config['device'])
        _train_t = train_t[train_indices].to(config['device'])

        _valid_X = train_X[valid_indices].to(config['device'])
        _valid_W = train_W[valid_indices].to(config['device'])
        _valid_t = train_t[valid_indices].to(config['device'])

        train_dataset = torch.utils.data.TensorDataset(
            _train_X, _train_t, _train_W)
        valid_dataset = torch.utils.data.TensorDataset(
            _valid_X, _valid_t, _valid_W)
        valid_iter = DataLoader(
            valid_dataset, batch_size=config['batchsize_valid'])
        model = build_model(config, embedding)
        model = model.to_device(config['device'])
        optimizer = build_optimizer(config, model)
        train_result = ClassificationResult('train', config['outdir'])
        valid_result = ClassificationResult('valid', config['outdir'])

        start = time.time()
        for epoch in range(config['epochs']):
            epoch_start = time.time()
            sampler = build_sampler(
                epoch, train_df.weights[train_indices].values)
            train_iter = DataLoader(
                train_dataset, sampler=sampler, drop_last=True,
                batch_size=config['batchsize'], shuffle=sampler is None)

            # Training loop
            for batch in tqdm(train_iter, desc='train', leave=False):
                model.train()
                optimizer.zero_grad()
                loss, output = model.calc_loss(*batch)
                loss.backward()
                optimizer.step()
                train_result.add_record(**output)
            train_result.calc_score(epoch)

            # Validation loop
            for batch in tqdm(valid_iter, desc='valid', leave=False):
                model.eval()
                loss, output = model.calc_loss(*batch)
                valid_result.add_record(**output)
            valid_result.calc_score(epoch)
            summary = pd.DataFrame([
                train_result.summary.iloc[-1],
                valid_result.summary.iloc[-1],
            ]).set_index('name')
            epoch_time = time.time() - epoch_start
            tqdm.write(f'\n###  cv: {i_cv} / {config["cv"]}, epoch {epoch}, '
                       f'time: {epoch_time}')
            tqdm.write(str(summary))

            # Case: updating the best score
            if epoch == valid_result.best_epoch:
                best_models[i_cv] = deepcopy(model)

        valid_result.elapsed_time = time.time() - start
        train_results.append(train_result)
        valid_results.append(valid_result)

        # Predict submit datasets
        submit_y = []
        for batch in tqdm(submit_iter, desc='submit', leave=False):
            model.eval()
            submit_y.append(best_models[i_cv].predict_proba(batch))
        submit_ys[i_cv] = np.concatenate(submit_y)

        # Predict testsets
        test_y = []
        for batch in tqdm(test_iter, desc='test', leave=False):
            model.eval()
            test_y.append(best_models[i_cv].predict_proba(batch))
        test_ys[i_cv] = np.concatenate(test_y)

    test_y = np.array(list(test_ys.values()), 'f').mean(axis=0)
    test_result = classification_metrics(test_y, test_t)

    scores = dict(
        valid_fbeta=np.array([r.best_fbeta for r in valid_results]).mean(),
        valid_epoch=np.array([r.best_epoch for r in valid_results]).mean(),
        valid_threshold=np.array([
            r.best_threshold for r in valid_results]).mean(),
        elapsed_time=np.array([r.elapsed_time for r in valid_results]).mean(),
        test_fbeta=test_result['fbeta'],
        test_threshold=test_result['threshold'],
    )
    print(scores)
    return scores


if __name__ == '__main__':
    main()