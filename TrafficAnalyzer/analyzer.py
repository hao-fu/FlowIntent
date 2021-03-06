from utils import set_logger
import os
import logging
from multiprocessing import Manager, Pool
from pcap_processor import flows2json, logger as pcap_proc_log
from learner import Learner
import numpy as np
import json
from argparse import ArgumentParser
from sklearn.linear_model import LogisticRegression
from sklearn import svm
import scipy.spatial.distance as ssd
from scipy import sparse
from scipy.cluster.hierarchy import dendrogram, linkage
from sklearn.model_selection import GridSearchCV
import pickle
from statistics import jaccard
import re
from sklearn.cluster import MeanShift
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs
from os_urlpattern.formatter import pformat
from os_urlpattern.pattern_maker import PatternMaker
from scipy.cluster.hierarchy import fcluster

logger = set_logger('Analyzer', 'INFO')


class Analyzer:
    # We currently do not consider indirect leakage in positive samples.
    # Indirect leakage: first leverage legal map sdk to get position description (such as city name),
    # then transfer the description out.
    map_sdk_urls = ['map.baidu.com', 'amap.com', 'maps.googleapis.com', 'maps.google.com', 'go2map.com',
                    'weather', 'gismeteo.ru']
    numeric_features = ['frame_num', 'up_count', 'non_http_num', 'len_stat', 'epoch_stat',
                        'up_stat', 'down_stat']

    @staticmethod
    def filter_url_words(url: str):
        parsed_uri = urlparse(url)
        host = parsed_uri.netloc.rsplit('.', 1)[0]  # this will remove url extensions, such as "com", "cn", etc
        path = parsed_uri.path
        query = parsed_uri.query
        float_pattern = re.compile('^.*[0-9]\\.[0-9]*')
        if re.match(float_pattern, query) is not None or re.match(float_pattern, path):
            # check whether the query/path contains float number, which could be a longitude/latitude.
            query += '_hasfloat'
        path = ''.join([i for i in path if not i.isdigit()])  # remove the digits in path
        query = ''.join([i for i in query if not i.isdigit()])  # remove the digits in query
        return host + path + '?' + query

    @staticmethod
    def pred_pos_contexts(pred_contexts_path):
        """
        Retrieve the predicted positive (abnormal) "contexts" using the voting results given by ContextProcessor.
        :param pred_contexts_path: Where the predicted contexts locate.
        :return pred_pos: predicted positive contexts.
        """
        with open(os.path.join(pred_contexts_path, 'contexts.json'), 'r', errors='ignore') as infile:
            contexts = json.load(infile)
            logger.info('The number of contexts: %d', len(contexts))
            pred_pos = []
            with open(os.path.join(pred_contexts_path, 'folds.json'), 'r', errors='ignore') as json_file:
                folds = json.load(json_file)
                for fold_id in folds:
                    pred_pos.extend([contexts[context] for context in folds[fold_id]['vot_pred_pos']])
                logger.debug(pred_pos)
            return pred_pos

    @staticmethod
    def filter_pos_flows(flow):
        u = flow['url']
        if u.endswith('.png') or u.endswith('.jpg') or u.endswith('.gif'):
            # TaintDroid may generate fp for figure urls.
            return True
        if Analyzer.map_sdk_urls is None:
            return False
        for url in Analyzer.map_sdk_urls:
            if url in flow['url']:
                return True

    @staticmethod
    def sens_flow_jsons(contexts: [{}], filter_flow) -> []:
        """
        Given contexts, get the corresponding sens_http_flows.json specified in context['dir'] field.
        :param contexts:
        :param filtered_urls: Ignore the flows who follow the rules specified inside.
        :return:
        """
        jsons = []
        for context in contexts:
            context_dir = context['dir']
            logger.debug(context_dir)
            for root, dirs, files in os.walk(context_dir):
                for file in files:
                    if not file.endswith('_sens_http_flows.json'):
                        continue
                    with open(os.path.join(root, file), 'r', encoding="utf8", errors='ignore') as infile:
                        flows = json.load(infile)
                        for flow in flows:
                            if not filter_flow(flow):
                                # The ground truth label, which is defined by "context" label.
                                flow['real_label'] = context['label']
                                jsons.append(flow)
        logger.info('The number of flows: %d', len(jsons))
        return jsons

    @staticmethod
    def gen_docs(jsons: [{}], char_wb: bool = False, add_taint: bool = False) -> [Learner.LabelledDocs]:
        """
        Generate string list from the flow URLs.
        :param jsons: The flow jsons.
        :param char_wb:
        :param add_taint: Whether add taints as tokens.
        :return:
        """
        docs = []
        taint_counts = 0
        for flow in jsons:
            line = Analyzer.filter_url_words(flow['url'])
            if '_' in flow['taint']:
                taint_counts += 1
            if add_taint:
                line = line + ' ' + 't_' + flow['taint']
            label = 1 if flow['label'] == '1' else 0
            real_label = 1 if flow['real_label'] == '1' else 0
            if real_label != label:
                logger.info("Flow's real label does not match the training label for %s, real_label = %d label = %d",
                            flow['url'], real_label, label)
            numeric = [flow[name] for name in Analyzer.numeric_features]
            docs.append(Learner.LabelledDocs(line, label, numeric, real_label, char_wb=char_wb))
        logger.info('The number of flows who have more than 1 taints: %d', taint_counts)
        return docs

    @staticmethod
    def gen_instances(positive_flows: list, negative_flows: list, simulate: bool = False, char_wb: bool = False,
                      add_taint: bool = False) -> (
            list, [[float]], np.array, np.array):
        """
        Generate the instances for ML from the given flows.
        :rtype 'Tuple[list, List[List[float]], ndarray, ndarray]
        :param positive_flows:
        :param negative_flows:
        :param simulate: Whether generate the simulated random flows.
        :param char_wb: Whether add a space before and after each token.
        :param add_taint: Whether add taints as tokens.
        :return:
        """
        logger.info('lenPos: %d', len(positive_flows))
        logger.info('lenNeg: %d', len(negative_flows))
        docs = Analyzer.gen_docs(positive_flows, char_wb, add_taint=add_taint)
        docs = docs + (Analyzer.gen_docs(negative_flows, char_wb, add_taint=add_taint))
        if simulate and len(negative_flows) == 0:
            docs = docs + Learner.simulate_flows(len(positive_flows), 0)
        samples = []
        samples_num = []
        labels = []
        real_labels = []
        for doc in docs:
            samples.append(doc.doc)
            numeric_fea_val = []
            for x in doc.numeric_features:
                if isinstance(x, list):
                    for val in x:
                        if val == '?':
                            logger.warning('Unknown value appeared in stats feature!')
                            val = 0.0
                        numeric_fea_val.append(float(val))
                else:
                    numeric_fea_val.append(float(x))
            samples_num.append(numeric_fea_val)
            labels.append(doc.label)
            real_labels.append(doc.real_label)
            logger.debug(str(doc.label) + ": " + doc.doc)

        return samples, samples_num, np.array(labels), np.array(real_labels)

    @staticmethod
    def metrics(y_plabs, y_test, test_index=None, result=None, label_type=0):
        tp = len(np.where((y_plabs == 1) & (y_test == 1))[0])
        tn = len(np.where((y_plabs == label_type) & (y_test == label_type))[0])
        fp_i = np.where((y_plabs == 1) & (y_test == label_type))[0]
        fp = len(fp_i)
        fn_i = np.where((y_plabs == label_type) & (y_test == 1))[0]
        fn = len(fn_i)
        accuracy = float(tp + tn) / float(tp + tn + fp + fn)
        if tp + fp == 0:
            logger.warn('Zero positive! All test samples are labelled as negative!')
            precision = 0
        else:
            precision = float(tp) / float(tp + fp)
        if fn + tn == 0:
            logger.warn('Zero negative! All test samples are labelled as positive!')
        if fn + tn == 0:
            logger.warn('Recall is Zero! tp + fn == 0!')
            recall = 0
        else:
            recall = float(tp) / float(tp + fn)
        if precision == 0 and recall == 0:
            logger.warn('Both precision and recall is zero!')
            f_score = 0
        else:
            f_score = 2 * (precision * recall) / (precision + recall)
        if result is not None:
            result['fp_item'] = test_index[fp_i]
            result['fn_item'] = test_index[fn_i]
        return accuracy, precision, recall, f_score

    @staticmethod
    def cross_validation(X, y, real_labels, clf, fold=5, label_type=0):
        folds = Learner.n_folds(X, y, fold=fold)
        results = dict()
        results['fold'] = []
        res = dict()
        res['scores'] = []
        res['true_scores'] = []
        res['precision'] = []
        res['true_precision'] = []
        res['recall'] = []
        res['true_recall'] = []
        scores = res['scores']
        true_scores = res['true_scores']
        for fold_id in folds:
            fold = folds[fold_id]
            result = dict()
            train_index = fold['train_index']
            test_index = fold['test_index']
            X_train, X_test = X[train_index], X[test_index]
            # TODO The real label here is currently determined by manually labelled contexts,
            #  but neg contexts may generate pos flows.
            y_train, y_test = y[train_index], real_labels[test_index]
            y_train_true = real_labels[train_index]
            # train the classifier
            clf.fit(X_train, y_train)
            # make the predictions
            predicted = clf.predict(X_test)
            y_plabs = np.squeeze(predicted)
            accuracy, precision, recall, f_score = Analyzer.metrics(y_plabs, y_test, test_index, result,
                                                                    label_type=label_type)
            logger.info("Accuracy: %f", accuracy)
            result['f_score'] = f_score
            result['precision'] = precision
            result['recall'] = recall
            results['fold'].append(result)
            res['precision'].append(precision)
            res['recall'].append(recall)
            scores.append(f_score)
            logger.info("F-score: %f Precision: %f Recall: %f", f_score, precision, recall)
            # train the classifier
            clf.fit(X_train, y_train_true)
            # make the predictions
            predicted = clf.predict(X_test)
            y_plabs = np.squeeze(predicted)
            accuracy, precision, recall, f_score = Analyzer.metrics(y_plabs, y_test)
            logger.info("True Accuracy: %f", accuracy)
            logger.info("True F-score: %f Precision: %f Recall: %f", f_score, precision, recall)
            true_scores.append(f_score)
            res['true_precision'].append(precision)
            res['true_recall'].append(recall)
        results['mean_scores'] = np.mean(scores)
        results['std_scores'] = np.std(scores)
        results['mean_precision'] = np.mean(res['precision'])
        results['mean_recall'] = np.mean(res['recall'])
        results['true_mean_scores'] = np.mean(true_scores)
        results['true_mean_precision'] = np.mean(res['true_precision'])
        results['true_mean_recall'] = np.mean(res['true_recall'])
        logger.info('\n')
        logger.info('mean score: %f', results['mean_scores'])
        logger.info('true mean score: %f', results['true_mean_scores'])
        logger.info('mean precision: %f', results['mean_precision'])
        logger.info('true mean precision: %f', results['true_mean_precision'])
        logger.info('mean recall: %f', results['mean_recall'])
        logger.info('true mean recall: %f\n', results['true_mean_recall'])
        return results

    @staticmethod
    def anomaly_detection(X, y, real_labels, fold=5):
        pos = np.where(y == 1)
        X_pos, real_pos = X[pos], real_labels[pos]
        X_neg = X[np.where(y == 0)]
        # Divide X_pos into folds for cross-validation.
        folds = Learner.n_folds(X_pos, np.ones(X_pos.shape[0]), fold=fold)
        results = dict()
        results['fold'] = []
        scores = []
        true_scores = []
        # define outlier/anomaly detection methods to be compared
        outliers_fraction = 0.27
        anomaly_algorithms = [
            # ("Robust covariance", EllipticEnvelope(contamination=outliers_fraction)),
            # ("One-Class SVM", svm.OneClassSVM(nu=outliers_fraction, kernel="rbf",
            # gamma=1e-09)),
            ("MeanShift", MeanShift()),
            # ("Isolation Forest", IsolationForest(behaviour='new',
            #                                      contamination=outliers_fraction,
            #                                      random_state=42)),
            # ("Local Outlier Factor", LocalOutlierFactor(
            # n_neighbors=35, contamination=outliers_fraction))
        ]
        for fold_id in folds:
            fold = folds[fold_id]
            for name, algorithm in anomaly_algorithms:
                logger.info('--------------------%s-------------------', name)
                result = dict()
                train_index = fold['train_index']
                test_index = fold['test_index']
                X_train, X_test = X_pos[train_index], X_pos[test_index]
                # TODO The real label here is currently determined by manually labelled contexts.
                y_train, y_test = y[train_index], real_labels[test_index]
                for i in range(y_test.shape[0]):
                    y_test[i] = -1 if y_test[i] == 0 else y_test[i]
                X_test = np.row_stack([X_test.toarray(), X_neg.toarray()])
                y_neg = -1 * np.ones(X_neg.shape[0])
                y_test = np.concatenate((y_test, y_neg), axis=0)
                if isinstance(algorithm, svm.OneClassSVM):
                    # y_train_true = real_pos[train_index]
                    grid = {'gamma': np.logspace(-9, 3, 13), 'nu': np.linspace(0.01, 0.99, 99)}
                    search = GridSearchCV(algorithm, grid, iid=False, cv=5, return_train_score=False,
                                          scoring='accuracy')
                    search.fit(X_train, y_train)
                    logger.debug("Best parameter (CV score=%0.3f):" % search.best_score_)
                    logger.debug(search.best_params_)
                    # train the classifier
                    # TODO nu should be determined by the context classification results:
                    #  the percentage of neg flows appeared under pos contexts.
                    # algorithm.fit(X_train.toarray())
                    # make the predictions
                    algorithm = search
                elif isinstance(algorithm, MeanShift):
                    algorithm.fit(X_train.toarray())
                else:
                    algorithm.fit(X_train)
                predicted = algorithm.predict(X_test)
                y_plabs = np.squeeze(predicted)
                # for i in range(len(real_labels)):
                #     added = np.array([test_index.shape[0]])
                #     test_index = np.concatenate((test_index, added), axis=0)
                logger.debug(y_plabs)
                logger.debug(y_test)
                accuracy, precision, recall, f_score = Analyzer.metrics(y_plabs, y_test, label_type=-1)
                logger.info("Accuracy: %f", accuracy)
                result['f_score'] = f_score
                results['fold'].append(result)
                scores.append(f_score)
                logger.info("F-score: %f Precision: %f Recall: %f", f_score, precision, recall)
        results['mean_scores'] = np.mean(scores)
        results['std_scores'] = np.std(scores)
        logger.info('mean score: %f', results['mean_scores'])
        logger.info('true mean score: %f', np.mean(true_scores))
        return results

    @staticmethod
    def url_clustering(pos_flows: [{}]):
        pattern_maker = PatternMaker()
        c = 0
        for flow in pos_flows:
            url = str(flow['url'])
            if url.endswith('&'):
                url = url[:-1]
            u = urlparse(url)
            query = parse_qs(u.query)
            for q in query:
                query[q] = ' '
            u = u._replace(query=urlencode(query, True))
            u = urlunparse(u)
            try:
                pattern_maker.load(u)
                c += 1
            except Exception as e:
                logger.warning('%s: %s', str(e), u)
        patterns = []
        for url_meta, clustered in pattern_maker.make():
            for pattern in pformat('pattern', url_meta, clustered):
                if pattern is not None and pattern != '/':
                    logger.debug(pattern)
                    patterns.append(pattern)
        logger.info('The number of patterns %d, from %d flows', len(patterns), c)
        return patterns

    @staticmethod
    def url_pattern2set(pattern: str):
        assert isinstance(pattern, str)
        url = pattern.replace('[', '').replace(']', '').replace('\\', '')
        u = urlparse(url)
        query = parse_qs(u.query)
        a = set()
        for q in query:
            a.add(q)
            logger.debug(q)
        for p in u.path.replace('.', '/').replace('-', '').split('/'):
            if p is not '':
                a.add(p)
                logger.debug(p)
        return a

    @staticmethod
    def url_pattern_dist(a: str, b: str):
        """
        URL pattern distance.
        :param a: a url signature
        :param b: another url signature
        :return: the distance value
        """
        return 1 - jaccard(Analyzer.url_pattern2set(a), Analyzer.url_pattern2set(b))

    @staticmethod
    def signature_dendrogram(flows: [{}]):
        pc = Analyzer.url_clustering(flows)
        pc = sorted(pc)
        dm = np.asarray([[Analyzer.url_pattern_dist(p1, p2) for p2 in pc] for p1 in pc])
        dm = sparse.csr_matrix(dm)
        dm = ssd.squareform(dm.todense())
        Z = linkage(dm)
        Learner.fancy_dendrogram(Z, leaf_rotation=90., leaf_font_size=8)
        for i in range(len(pc)):
            logger.info('Signature %d: %s', i, pc[i])
        return Z, pc

    @staticmethod
    def flow_cluster(flows: [{}], max_d: float):
        Z, pc = Analyzer.signature_dendrogram(flows)
        clusters = fcluster(Z, max_d, criterion='distance')
        cls = dict()
        i = 0
        for c in clusters:
            # Notice that the value of c has noting to do the original signature index.
            logger.info('%d %d %s', i, c - 1, pc[i])
            if c not in cls:
                cls[c] = []
            cls[c].append(pc[i])
            i += 1
        logger.info(clusters)
        sig_cls = []
        for sigs in cls.values():
            logger.debug(sigs)
            s = Analyzer.url_pattern2set(sigs[0])
            for i in range(1, len(sigs)):
                s = s.intersection(Analyzer.url_pattern2set(sigs[i]))
            if len(s) > 2:
                sig_cls.append(s)
                logger.info(s)
            else:
                for s in sigs:
                    s = Analyzer.url_pattern2set(s)
                    if len(s) >= 2:
                        sig_cls.append(s)
                        logger.info(s)
        logger.info('The number of clusters: %d', len(sig_cls))
        return sig_cls

    @staticmethod
    def fcluster_predict(sigs: [set], flows: [{}]):
        matched = {}
        for f in flows:
            u = Analyzer.url_pattern2set(f['url'])
            for s in sigs:
                if len(u.intersection(s)) == len(s):
                    logger.info('Matched %s: %s', s, f['url'])
                    matched[f['url']] = s
                    break
            if f['url'] not in matched:
                logger.debug('%s does not match', f['url'])
        return matched


def flows2jsons(negative_pcap_dir, label, json_ext, visited_pcap, fn_filter='filter', has_sub_dir=False):
    if has_sub_dir:
        for root, dirs, files in os.walk(negative_pcap_dir):
            for file in files:
                if file.endswith('.pcap') and file not in visited_pcap:
                    visited_pcap[file] = 1
                    flows2json(root, file, fn_filter=fn_filter, label=label, json_ext=json_ext)
        return
    for filename in os.listdir(negative_pcap_dir):
        if filename not in visited_pcap:
            visited_pcap[filename] = 1
            flows2json(negative_pcap_dir, filename, fn_filter=fn_filter, label=label, json_ext=json_ext)


def flow2json_mp_wrapper(args):
    flows2jsons(*args)


def gen_neg_flow_jsons(negative_pcap_dir, proc_num=4, has_sub_dir=False):
    """
    :param negative_pcap_dir: The directory of labelled negative (normal) pcaps.
    :param proc_num:
    :param has_sub_dir: Whether has sub directories.
    """
    visited = Manager().dict()
    p = Pool(proc_num)
    p.map(flow2json_mp_wrapper, [(negative_pcap_dir, '0', '_http_flows.json', visited, None, has_sub_dir)] * proc_num)
    p.close()


def preprocess(negative_pcap_dir, sub_dir_name=''):
    """
    Extract pos and neg pcaps from labelled context directories, and then transform them into jsons.
    :param negative_pcap_dir: The directory of labelled negative (normal) pcaps.
    :param sub_dir_name:
    """
    # Positive/Abnormal pcaps.
    contexts_dir = os.path.join("../AppInspector/data/", sub_dir_name)
    logger.info('The contexts are stored at %s', os.path.abspath(contexts_dir))
    contexts = Analyzer.pred_pos_contexts(contexts_dir)
    positive_flows = Analyzer.sens_flow_jsons(contexts, Analyzer.filter_pos_flows)
    for flow in positive_flows:
        # The label given by the prediction of AppInspector, may not be as same as the ground truth.
        flow['label'] = '1'

    # Negative/Normal pcaps.
    # They have no relationship with "context" defined in AppInspector, just a bunch of normal flows.
    negative_flows = []
    for file in os.listdir(negative_pcap_dir):
        if file.endswith('_http_flows.json'):
            with open(os.path.join(negative_pcap_dir, file), 'r', encoding="utf8", errors='ignore') as infile:
                flows = json.load(infile)
                for flow in flows:
                    # The context label is as same as the ground truth since they are not labelled by AppInspector.
                    flow['real_label'] = '0'
                    negative_flows.append(flow)

    return positive_flows, negative_flows


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("-d", "--dir", dest="neg_pcap_dir",
                        help="the full path of the dir that stores pcap files labelled as normal")
    parser.add_argument("-j", "--json", dest="gen_json", action='store_true',
                        help="if the jsons of the negative flows are not generated, generate")
    parser.add_argument("-n", "--numeric", dest="numeric", action='store_true',
                        help="use numeric features only")
    parser.add_argument("-l", "--log", dest="log", default='INFO',
                        help="the log level, such as INFO, DEBUG")
    parser.add_argument("-p", "--proc", dest="proc_num", default=4,
                        help="the number of processes used in multiprocessing")
    parser.add_argument("-sub", "--subdir", dest="sub_dir", default='',
                        help="the sub dir name that stores contexts")
    parser.add_argument("-u", "--unsuper", dest="unsupervised", action='store_true',
                        help="whether perform unsupervised learning")
    parser.add_argument("-s", "--save", dest="save_dir_path", default='',
                        help="save the predictor to which directory")
    parser.add_argument("-f", "--fname", dest="fname", default='test',
                        help="the file name of the saved stuff")
    parser.add_argument("-a", "--all", dest="all_feature", action='store_true',
                        help="use both statistical and lexical features, which needs more memory")
    parser.add_argument("-c", "--cluster", dest="cluster_max_d", default=0,
                        help="the max distance threshold for flow clustering")
    parser.add_argument("-t", "--taint", dest="add_taint", action='store_true',
                        help="whether add taint info into feature space")
    args = parser.parse_args()

    if args.log != 'INFO':
        logger = set_logger('Analyzer', args.log)
    pcap_proc_log.setLevel(logging.INFO)
    neg_pcap_dir = args.neg_pcap_dir
    logger.info('The negative pcaps are stored at: %s', neg_pcap_dir)
    if args.gen_json:
        gen_neg_flow_jsons(neg_pcap_dir, args.proc_num)
    pos_flows, neg_flows = preprocess(neg_pcap_dir, sub_dir_name=args.sub_dir)
    if args.cluster_max_d != 0:
        d = float(args.cluster_max_d)
        logger.info('--------------------Flow Clustering--------------------')
        logger.info('The max distance threshold %f', d)
        sigs = Analyzer.flow_cluster(pos_flows, d)
        fps = Analyzer.fcluster_predict(sigs, neg_flows)
        logger.info('The number of false positives: %d, from %d flows', len(fps), len(neg_flows))
        exit(0)
    text_fea, numeric_fea, y, true_labels = Analyzer.gen_instances(pos_flows, neg_flows, char_wb=False, simulate=False,
                                                                   add_taint=args.add_taint)
    solver = 'newton-cg'  # 'liblinear'
    penalty = 'l2'
    if not args.numeric:
        X, feature_names, vec = Learner.LabelledDocs.vectorize(text_fea, tf=False)
        if args.all_feature:
            X = X.toarray()
            X = np.hstack([X, numeric_fea])
    else:
        feature_names = Analyzer.numeric_features
        X = np.hstack([numeric_fea])
    logger.info('--------------------Logistic Regression-------------------')
    if penalty is None or penalty == '':
        clf = LogisticRegression(solver=solver, class_weight='balanced', C=1e42)
    else:
        clf = LogisticRegression(solver=solver, penalty=penalty, class_weight='balanced')
    Analyzer.cross_validation(X, y, true_labels, clf)
    if args.save_dir_path != '':
        clf.fit(X, y)
        if not args.numeric and not args.all_feature:
            top_n = -100
            top_n = np.argpartition(clf.coef_[0], top_n)[top_n:]
            logger.info(np.array(feature_names)[top_n])
        os.makedirs(args.save_dir_path, exist_ok=True)
        model_path = os.path.join(args.save_dir_path, args.fname + '.model')
        with open(model_path, 'wb') as fid:
            pickle.dump(clf, fid)
            logger.info('The predictor is saved at %s', os.path.abspath(model_path))
        if not args.numeric:
            vec_path = os.path.join(args.save_dir_path, args.fname + '.vec')
            with open(vec_path, 'wb') as fid:
                pickle.dump(vec, fid)
                logger.info('The predictor is saved at %s', os.path.abspath(vec_path))
    if args.unsupervised:
        logger.info('--------------------Unsupervised Learning-------------------')
        Analyzer.anomaly_detection(X, y, true_labels)
