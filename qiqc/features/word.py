from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from gensim.models import Word2Vec, FastText
from scipy.stats import chi2_contingency

from qiqc.utils import ApplyNdArray


class WordVocab(object):

    def __init__(self):
        self.counter = Counter()
        self.n_documents = 0
        self._counters = {}
        self._n_documents = defaultdict(int)

    def __len__(self):
        return len(self.token2id)

    def add_documents(self, documents, name):
        self._counters[name] = Counter()
        for document in documents:
            bow = dict.fromkeys(document, 1)
            self._counters[name].update(bow)
            self.counter.update(bow)
            self.n_documents += 1
            self._n_documents[name] += 1

    def build(self):
        counter = dict(self.counter.most_common())
        self.word_freq = {
            **{'<PAD>': 0},
            **counter,
        }
        self.token2id = {
            **{'<PAD>': 0},
            **{word: i + 1 for i, word in enumerate(counter)}
        }


class WordFeatureTransformer(object):

    def __init__(self, vocab, initialW, min_count):
        self.vocab = vocab
        self.word_freq = vocab.word_freq
        self.token2id = vocab.token2id
        self.initialW = initialW
        self.finetuned_vectors = None
        self.min_count = min_count
        self.n_embed = self.initialW.shape[1]

        self.unk = (initialW == 0).all(axis=1)
        self.known = ~self.unk
        self.lfq = np.array(list(vocab.word_freq.values())) < min_count
        self.hfq = ~self.lfq
        self.mean = initialW[self.known].mean()
        self.std = initialW[self.known].std()
        self.extra_features = None

    def build_fillvalue(self, mode, n_fill):
        assert mode in {'zeros', 'mean', 'noise'}
        if mode == 'zeros':
            return np.zeros(self.n_embed, 'f')
        elif mode == 'mean':
            return self.initialW.mean(axis=0)
        elif mode == 'noise':
            return np.random.normal(
                self.mean, self.std, (n_fill, self.n_embed))

    def finetune_skipgram(self, df, params, fill_unk):
        tokens = df.tokens.values
        model = Word2Vec(**params)
        model.build_vocab_from_freq(self.word_freq)
        initialW = self.initialW.copy()
        initialW[self.unk] = self.build_fillvalue(
            fill_unk, initialW[self.unk].shape)
        idxmap = np.array(
            [self.vocab.token2id[w] for w in model.wv.index2entity])
        model.wv.vectors[:] = initialW[idxmap]
        model.trainables.syn1neg[:] = initialW[idxmap]
        model.train(tokens, total_examples=len(tokens), epochs=model.epochs)
        finetunedW = self.initialW.copy()
        finetunedW[idxmap] = model.wv.vectors
        return finetunedW

    def finetune_fasttext(self, df, params, fill_unk):
        tokens = df.tokens.values
        model = FastText(**params)
        model.build_vocab_from_freq(self.word_freq)
        initialW = self.initialW.copy()
        initialW[self.unk] = self.build_fillvalue(
            fill_unk, initialW[self.unk].shape)
        idxmap = np.array(
            [self.vocab.token2id[w] for w in model.wv.index2entity])
        model.wv.vectors[:] = initialW[idxmap]
        model.wv.vectors_vocab[:] = initialW[idxmap]
        model.trainables.syn1neg[:] = initialW[idxmap]
        model.train(tokens, total_examples=len(tokens), epochs=model.epochs)
        finetunedW = np.zeros((initialW.shape), 'f')
        for i, word in enumerate(self.vocab.token2id):
            if word in model.wv:
                finetunedW[i] = model.wv.get_vector(word)
        return finetunedW

    def standardize(self, embedding):
        indices = (embedding != 0).all(axis=1)
        _embedding = embedding[indices]
        mean, std = _embedding.mean(axis=0), _embedding.std(axis=0)
        standardized = embedding.copy()
        standardized[indices] = (embedding[indices] - mean) / std
        return standardized

    def standardize_freq(self, embedding):
        indices = (embedding != 0).all(axis=1)
        _embedding = embedding[indices]
        freqs = np.array(list(self.vocab.word_freq.values()))[indices]
        weighted_embedding = _embedding * freqs[:, None]
        mean = weighted_embedding.sum(axis=0) / freqs.sum()
        se = freqs[:, None] * (_embedding - mean) ** 2
        std = np.sqrt(se.sum(axis=0) / freqs.sum())
        standardized = embedding.copy()
        standardized[indices] = (embedding[indices] - mean) / std
        return standardized

    def build_extra_features(self, df, config):
        extra_features = np.empty((len(self.vocab.token2id), 0))
        if 'chi2' in config:
            chi2_features = self.build_chi2_features(df)
            extra_features = np.concatenate(
                [extra_features, chi2_features], axis=1)
        if 'idf' in config:
            idf_features = self.build_idf_features(df, onehot=False)
            extra_features = np.concatenate(
                [extra_features, idf_features], axis=1)
        if 'idf_onehot' in config:
            idf_features = self.build_idf_features(df, onehot=True)
            extra_features = np.concatenate(
                [extra_features, idf_features], axis=1)
        if 'unk' in config:
            unk_features = self.build_unk_features(df)
            extra_features = np.concatenate(
                [extra_features, unk_features], axis=1)
        return extra_features

    # TODO: Fix to build dictionary for calculation efficiency
    def build_chi2_features(self, df, threshold=0.01):
        vocab_pos = self.vocab._counters['train_pos']
        vocab_neg = self.vocab._counters['train_neg']

        counts = pd.DataFrame({'tokens': list(self.vocab.token2id.keys())})
        counts['TP'], counts['FP'] = 0, 0

        idxmap = [self.vocab.token2id[k] for k, v in vocab_pos.items()]
        counts.loc[idxmap, 'TP'] = list(vocab_pos.values())
        idxmap = [self.vocab.token2id[k] for k, v in vocab_neg.items()]
        counts.loc[idxmap, 'FP'] = list(vocab_neg.values())

        counts['FN'] = self.vocab._n_documents['train_pos'] - counts.TP
        counts['TN'] = self.vocab._n_documents['train_neg'] - counts.FP
        counts['TP/.P'] = counts.TP / (counts.TP + counts.FP)
        counts['class_ratio'] = self.vocab._n_documents['train_pos'] / \
            self.vocab.n_documents
        counts['df'] = (counts.TP + counts.FP) / self.vocab.n_documents

        def chi2_func(arr):
            TP, FP, FN, TN = arr
            if TN == 0 or TP == 0:
                return np.inf
            else:
                return chi2_contingency(arr.reshape(2, 2))[1]

        threshold = 0.01
        min_count = 10

        apply_chi2 = ApplyNdArray(chi2_func, processes=1, dtype='f')
        counts['chi2_p'] = apply_chi2(
            counts[['TP', 'FP', 'FN', 'TN']].values)
        counts['chi2_label'] = 0
        is_important = (counts.chi2_p < threshold) & \
            (counts['TP/.P'] > counts.class_ratio) & (counts.TP >= min_count)
        counts.loc[is_important, 'chi2_label'] = 1

        return counts.feature[:, None]

    def build_idf_features(self, df, onehot=True):
        dfs = np.array(list(self.vocab.word_freq.values()))
        if onehot:
            dfs[dfs > 10] = 10
            features = np.eye(11)[:, 1:][dfs]
        else:
            dfs[0] = self.vocab.n_documents
            features = np.log(self.vocab.n_documents / dfs)
            features = features[:, None]
        return features

    def build_unk_features(self, df):
        features = self.unk.astype('f')
        features[0] = 0
        features = features[:, None]
        return features
