import numpy as np
from itertools import izip, izip_longest
from src.lib import logstats
from preprocess import markers
from src.basic.entity import is_entity
from src.model.evaluate import BaseEvaluator
from preprocess import Dialogue, price_filler
from src.basic.negotiation.price_tracker import PriceTracker

def get_evaluator(data_generator, model, splits=('test',), batch_size=1, verbose=True):
    if model.name in ('ranker-cheat', 'ranker-ir'):
        return RetrievalEvaluator(data_generator, model, splits, batch_size, verbose)
    elif model.name in ('ranker-encdec',):
        return EncDecRetrievalEvaluator(data_generator, model, splits, batch_size, verbose)
    elif model.name == 'lm':
        return LMEvaluator(data_generator, model, splits, batch_size, verbose)
    else:
        return Evaluator(data_generator, model, splits, batch_size, verbose)

# TODO: factor this
def pred_to_token(preds, stop_symbol, remove_symbols, textint_map, num_sents=None, prices=None):
    '''
    Convert integer predition to tokens. Remove PAD and EOS.
    preds: (batch_size, max_len)
    '''
    def find_stop(array, n):
        count = 0
        for i, a in enumerate(array):
            if a == stop_symbol:
                count += 1
                if count == n:
                    # +1: include </s>
                    return i + 1
        return None
    tokens = []
    entities = []
    if num_sents is None:
        num_sents = [1 for _ in preds]
    if prices:
        for pred, n, price in izip(preds, num_sents, prices):
            N = find_stop(pred, n)
            assert len(pred) == len(price)
            token_price = [(x, p) for x, p in izip(pred[:N], price[:N]) if not x in remove_symbols]
            s = textint_map.int_to_text([x[0] for x in token_price], prices=[x[1] for x in token_price])
    else:
        for pred, n in izip(preds, num_sents):
            s = textint_map.int_to_text([x for x in pred[:find_stop(pred, n)] if not x in remove_symbols])
            tokens.append(s)
    return tokens, entities if len(entities) > 0 else None

class Evaluator(BaseEvaluator):
    def _stop_symbol(self):
        return self.vocab.to_ind(markers.EOS)

    def _remove_symbols(self):
        inds = map(self.vocab.to_ind, (markers.PAD,))
        words = [markers.PAD]
        return inds + words

    def multi_ref_scores(self, batch_candidates, batch_candidate_scores, batch_preds, batch_targets, summary_map):
        best_candidates = []
        for candidates, scores, preds, target in izip(batch_candidates, batch_candidate_scores, batch_preds, batch_targets):
            candidates = [c['response'] for c in candidates if 'response' in c]
            assert len(candidates) == len(scores)
            scores = [sum(s) for s in scores]
            candidates = [self._process_target_tokens(c) for i, c in enumerate(candidates) if scores[i] > 0]
            candidates.append(target)

            self.preds.append(preds)
            self.references.append(candidates)
            self.targets.append([target])

            bleus = [self.sentence_bleu_score([preds], [c])[0] for c in candidates]
            most_similar_candidate = np.argmax(bleus)
            best_candidates.append(candidates[most_similar_candidate])
            self.most_similar_references.append([candidates[most_similar_candidate]])
            #weighted_bleu = bleus[most_similar_candidate] * float(sum(scores[most_similar_candidate]))
            #print bleus[most_similar_candidate]
            #if bleus[most_similar_candidate] < 0.5:
            #    print 'preds:', preds
            #    print 'cand:', candidates[most_similar_candidate]
            #    #for c in candidates:
            #    #    print c
            #weighted_bleu = self.sentence_multi_bleu_score(preds, candidates)

            #logstats.update_summary_map(summary_map, {'multi_score': weighted_bleu})

        return best_candidates

    def _generate_response(self, sess, dialogue_batch, summary_map):
        encoder_init_state = None
        for batch in dialogue_batch['batch_seq']:
            targets = batch['targets']
            max_len = targets.shape[1] + 10
            output_dict = self.model.generate(sess, batch, encoder_init_state, max_len, textint_map=self.data.textint_map)
            preds = output_dict['preds']
            prices = output_dict['prices']
            true_final_state = output_dict['true_final_state']
            encoder_init_state = true_final_state
            num_sents = np.sum(targets == self.stop_symbol, axis=1)
            pred_tokens, pred_entities = pred_to_token(preds, self.stop_symbol, self.remove_symbols, self.data.textint_map, num_sents=num_sents, prices=prices)

            # TODO: handle process consistently
            pred_tokens = [self._process_target_tokens(tokens) for tokens in pred_tokens]
            references = [self._process_target_tokens(tokens) for tokens in batch['decoder_tokens']]

            best_candidates = None
            #if 'token_candidates' in batch:
            #    best_candidates = self.multi_ref_scores(batch['token_candidates'], batch['candidate_scores'], pred_tokens, references, summary_map)

            # Metrics
            # Sentence bleu: only for verbose print
            bleu_scores = self.sentence_bleu_score(pred_tokens, references)
            self.update_bleu_stats(summary_map, pred_tokens, references)
            self.update_entity_stats(summary_map, pred_tokens, references, 'entity_')

            if self.verbose:
                #attn_scores = output_dict.get('attn_scores', None)
                #probs = output_dict.get('probs', None)
                self._print_batch(batch, pred_tokens, references, bleu_scores, best_candidates)

    def _process_target_tokens(self, tokens):
        remove_tokens = (markers.GO_B, markers.GO_S)
        process_entity = lambda e: e.canonical if e.canonical.type == 'price' else e.surface
        targets = [process_entity(token) if is_entity(token) else token for token in tokens if not token in remove_tokens]
        return targets

    def to_str(self, words):
        return ' '.join([str(w) for w in words if w not in self.remove_symbols])

    # TODO: refactor print batch
    def _print_batch(self, batch, preds, targets, bleu_scores, best_candidates=None):
        '''
        inputs are integers; targets and preds are tokens (converted in test_bleu).
        '''
        encoder_tokens = batch['encoder_tokens']
        inputs = batch['encoder_inputs']
        decoder_tokens = batch['decoder_tokens']
        kbs = batch['kbs']

        print '-------------- batch ----------------'
        for i, (target, pred, bleu) in enumerate(izip_longest(targets, preds, bleu_scores)):
            # Skip padded turns
            if len(decoder_tokens[i]) == 0:
                continue
            kb = kbs[i]
            kb.dump()
            #print 'RAW INPUT:', Dialogue.original_price(kb, encoder_tokens[i])
            #print 'RAW TARGET:', Dialogue.original_price(kb, target)
            #print 'RAW INPUT:', encoder_tokens[i]
            #print 'RAW TARGET:', target
            print '----------'
            if 'encoder_context' in batch:
                print 'CONTEXT:'
                for c in batch['encoder_context']:
                    print self.to_str(self.data.textint_map.int_to_text(c[i], 'encoding'))
            print 'INPUT:', self.to_str(self.data.textint_map.int_to_text(inputs[i], 'encoding'))
            print 'TARGET:', self.to_str(target)
            #print 'BEST CANDIDATES:', self.to_str(best_cand)
            print 'PRED:', self.to_str(pred)
            print 'BLEU:', bleu

    def get_stats(self, summary_map):
        output = super(Evaluator, self).get_stats(summary_map)
        output['entity_f1'] = self.get_f1(summary_map, 'entity_')
        #if 'multi_score' in summary_map:
        #    output['multi_score'] = summary_map['multi_score']['mean']
        output['multi_score'] = self.multi_bleu_score(self.preds, self.references)[0]
        output['single_score'] = self.multi_bleu_score(self.preds, self.targets)[0]
        output['most_similar_score'] = self.multi_bleu_score(self.preds, self.most_similar_references)[0]
        return output

    def stats2str(self, stats):
        s = [super(Evaluator, self).stats2str(stats)]
        for m in ('entity_f1',):
            s.append('%s=%.4f/%.4f/%.4f' % (m, stats[m][0], stats[m][1],stats[m][2]))
        if 'multi_score' in stats:
            s.append('%s=%.4f' % ('multi_score', stats['multi_score']))
            s.append('%s=%.4f' % ('single_score', stats['single_score']))
            s.append('%s=%.4f' % ('most_similar_score', stats['most_similar_score']))
        return ' '.join(s)

    # NOTE: both batch_preds and batch_targets must use canonical entity form: (name, type)
    def update_entity_stats(self, summary_map, batch_preds, batch_targets, prefix=''):
        def get_entity(x):
            return [e for e in x if is_entity(e)]
        pos_target = prefix + 'pos_target'
        pos_pred = prefix + 'pos_pred'
        tp = prefix + 'tp'
        for preds, targets in izip (batch_preds, batch_targets):
            preds = set(get_entity(preds))
            targets = set(get_entity(targets))
            # Don't record cases where no entity is presented
            if len(targets) > 0:
                logstats.update_summary_map(summary_map, {pos_target: len(targets), pos_pred: len(preds)})
                logstats.update_summary_map(summary_map, {tp: sum([1 if e in preds else 0 for e in targets])})

    def log_dict(self, stats):
        d = super(Evaluator, self).log_dict(stats)
        if 'entity_f1' in stats:
            precision, recall, f1 = stats['entity_f1']
            d.update({'entity_precision': precision, 'entity_recall': recall, 'entity_f1': f1})
        return d

class RetrievalEvaluator(Evaluator):
    def _generate_response(self, sess, dialogue_batch, summary_map):
        prev_turns = []
        for batch in dialogue_batch['batch_seq']:
            references = [self._process_target_tokens(tokens) for tokens in batch['decoder_tokens']]
            prev_turns.append([self._process_target_tokens(tokens) for tokens in batch['encoder_tokens']])
            output_dict = self.model.select(batch)
            pred_tokens = self._process_target_tokens(output_dict['responses'])

            if 'token_candidates' in batch:
                self.multi_ref_scores(batch['token_candidates'], batch['candidate_scores'], pred_tokens, summary_map)

            # Metrics
            # Sentence bleu: only for verbose print
            bleu_scores = self.sentence_bleu_score(pred_tokens, references)
            self.update_bleu_stats(summary_map, pred_tokens, references)
            self.update_entity_stats(summary_map, pred_tokens, references, 'entity_')

            if self.verbose:
                self._print_batch(batch, prev_turns, pred_tokens, references, bleu_scores, output_dict)
            prev_turns.append(references)

    def _print_batch(self, batch, prev_turns, preds, targets, bleu_scores, output_dict=None, results=None):
        '''
        inputs are integers; targets and preds are tokens (converted in test_bleu).
        '''
        encoder_tokens = batch['encoder_tokens']
        inputs = batch['encoder_inputs']
        decoder_tokens = batch['decoder_tokens']
        kbs = batch['kbs']

        print '-------------- batch ----------------'
        for i, (target, pred, bleu) in enumerate(izip_longest(targets, preds, bleu_scores)):
            # Skip padded turns
            if len(decoder_tokens[i]) == 0:
                continue
            kb = kbs[i]
            kb.dump()
            #print 'RAW INPUT:', Dialogue.original_price(kb, encoder_tokens[i])
            #print 'RAW TARGET:', Dialogue.original_price(kb, target)
            #print 'RAW INPUT:', encoder_tokens[i]
            #print 'RAW TARGET:', target
            print '----------'
            print 'CONTEXT:'
            for turn in prev_turns[-3:]:
                print self.to_str(turn[i])
            #print 'INPUT:', self.to_str(self.data.textint_map.int_to_text(inputs[i], 'encoding'))
            #print 'TARGET:', Dialogue.original_price(kb, target)
            #print 'PRED:', Dialogue.original_price(kb, pred)
            print 'TARGET:', self.to_str(target)
            print 'PRED:', self.to_str(pred)
            print 'BLEU:', bleu
            print 'ALL CANDIDATES:'
            for c in output_dict['candidates'][i]:
                if c != {}:
                    print 'Hits:', c['hits']
                    print 'Response:', self.to_str(c['response'])

class EncDecRetrievalEvaluator(RetrievalEvaluator):
    def _generate_response(self, sess, dialogue_batch, summary_map):
        encoder_init_state = None
        prev_turns  =[]
        for batch in dialogue_batch['batch_seq']:
            references = [self._process_target_tokens(tokens) for tokens in batch['decoder_tokens']]
            prev_turns.append([self._process_target_tokens(tokens) for tokens in batch['encoder_tokens']])
            # TODO
            output_dict = self.model.select(batch, encoder_init_state, self.data.textint_map)
            pred_tokens = output_dict['responses']
            pred_tokens = [self._process_target_tokens(tokens) for tokens in pred_tokens]
            encoder_init_state = output_dict['true_final_state']
            references = [self._process_target_tokens(tokens) for tokens in batch['decoder_tokens']]

            # TODO: refactor
            if 'token_candidates' in batch:
                self.multi_ref_scores(batch['token_candidates'], batch['candidate_scores'], pred_tokens, references, summary_map)

            # Metrics
            # Sentence bleu: only for verbose print
            bleu_scores = self.sentence_bleu_score(pred_tokens, references)
            self.update_bleu_stats(summary_map, pred_tokens, references)
            self.update_entity_stats(summary_map, pred_tokens, references, 'entity_')

            if self.verbose:
                self._print_batch(batch, prev_turns, pred_tokens, references, bleu_scores, output_dict)
            prev_turns.append(references)

    def _print_batch(self, batch, prev_turns, preds, targets, bleu_scores, output_dict=None, results=None):
        '''
        inputs are integers; targets and preds are tokens (converted in test_bleu).
        '''
        encoder_tokens = batch['encoder_tokens']
        inputs = batch['encoder_inputs']
        decoder_tokens = batch['decoder_tokens']
        kbs = batch['kbs']

        print '-------------- batch ----------------'
        for i, (target, pred, bleu) in enumerate(izip_longest(targets, preds, bleu_scores)):
            # Skip padded turns
            if len(decoder_tokens[i]) == 0:
                continue
            kb = kbs[i]
            kb.dump()
            #print 'RAW INPUT:', Dialogue.original_price(kb, encoder_tokens[i])
            #print 'RAW TARGET:', Dialogue.original_price(kb, target)
            #print 'RAW INPUT:', encoder_tokens[i]
            #print 'RAW TARGET:', target
            print '----------'
            print 'CONTEXT:'
            for turn in prev_turns[-3:]:
                print self.to_str(turn[i])
            #print 'INPUT:', self.to_str(self.data.textint_map.int_to_text(inputs[i], 'encoding'))
            #print 'TARGET:', Dialogue.original_price(kb, target)
            #print 'PRED:', Dialogue.original_price(kb, pred)
            print 'TARGET:', self.to_str(target)
            print 'PRED:', self.to_str(pred)
            #print 'BLEU:', bleu
            #print 'CHEAT:', self.to_str(output_dict['cheat_responses'][i])
            #print 'IR:', self.to_str(output_dict['IR_responses'][i])
            #print 'ALL CANDIDATES:'
            #for c in output_dict['candidates'][i]:
            #    if c != {}:
            #        #print 'Hits:', c['hits']
            #        print 'Response:', self.to_str(c['response'])


class LMEvaluator(Evaluator):
    def _stop_symbol(self):
        return self.vocab.to_ind(markers.EOS)

    def _remove_symbols(self):
        inds = map(self.vocab.to_ind, (markers.PAD,))
        words = [markers.PAD]
        return inds + words

    def _generate_response(self, sess, dialogue_batch, summary_map):
        init_state = None
        for batch in dialogue_batch['eval_batch_seq']:
            targets = batch['targets']
            max_len = targets.shape[1] + 10
            output_dict = self.model.generate(sess, batch, init_state, max_len, textint_map=self.data.textint_map)
            preds = output_dict['preds']
            true_final_state = output_dict['true_final_state']
            init_state = true_final_state
            num_sents = np.sum(targets == self.stop_symbol, axis=1)
            pred_tokens, pred_entities = pred_to_token(preds, self.stop_symbol, self.remove_symbols, self.data.textint_map, num_sents=num_sents)

            references = [self._process_target_tokens(tokens) for tokens in batch['decoder_tokens']]

            # Metrics
            # Sentence bleu: only for verbose print
            bleu_scores = self.sentence_bleu_score(pred_tokens, references)
            self.update_bleu_stats(summary_map, pred_tokens, references)
            self.update_entity_stats(summary_map, pred_tokens, references, 'entity_')

            if self.verbose:
                #attn_scores = output_dict.get('attn_scores', None)
                #probs = output_dict.get('probs', None)
                self._print_batch(batch, pred_tokens, references, bleu_scores)

    #def _print_batch(self, preds, targets, bleu_scores):
    #    for i, (target, pred, bleu) in enumerate(izip_longest(targets, preds, bleu_scores)):
    #        print '----------'
    #        #print 'INPUT:', self.to_str(self.data.textint_map.int_to_text(inputs[i], 'encoding'))
    #        print 'TARGET:', self.to_str(target)
    #        print 'PRED:', self.to_str(pred)
    #        print 'BLEU:', bleu
