"""
pmi: pmi_feldman
ppl_hyp: ppl_hypothesis_bias
ppl_pmi: ppl_based_pmi
ppl_marginal_bias

Remove pmi_lambda -> pmi_feldman_lambda
"""
import shutil
import json
from glob import glob


for i in glob('./experiments_results/logit/*/*/'):
    shutil.move('{}/pmi'.format(i), '{}/pmi_feldman'.format(i))
    shutil.move('{}/ppl_hyp'.format(i), '{}/ppl_hypothesis_bias'.format(i))
    shutil.move('{}/ppl_pmi'.format(i), '{}/ppl_based_pmi'.format(i))

for i in glob('./experiments_results/logit/*/*/*/config.json'):
    if 'pmi_feldman' in i:
        continue
    with open(i, 'r') as f:
        config = json.load(f)
    config.pop('pmi_lambda')
    with open(i, 'w') as f:
        json.dump(config, f)
    print(config.keys())


