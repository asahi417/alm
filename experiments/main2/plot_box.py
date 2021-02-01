import seaborn as sns
import matplotlib.pyplot as plt
import alm
import os
plt.rcParams.update({"text.usetex": True, "font.family": "sans-serif", "font.sans-serif": ["Helvetica"]})
os.makedirs('./experiments_results/summary/main2_figure', exist_ok=True)
export_prefix = 'main2'
df = alm.get_report(export_prefix=export_prefix)
df['accuracy'] = df['accuracy'].round(3) * 100
df['ppl_pmi_aggregation'] = df['ppl_pmi_aggregation'].apply(lambda x: r'val$_{0}{1}{2}$'.format(
    '{', (int(x.replace('index_', '')) + 1), '}') if 'index' in x else x)
df['negative_permutation_aggregation'] = df['negative_permutation_aggregation'].apply(lambda x: r'val$_{0}{1}{2}$'.format(
    '{', (int(x.replace('index_', '')) + 1), '}') if 'index' in x else x)
df['positive_permutation_aggregation'] = df['positive_permutation_aggregation'].apply(lambda x: r'val$_{0}{1}{2}$'.format(
    '{', (int(x.replace('index_', '')) + 1), '}') if 'index' in x else x)
data = ['sat', 'u2', 'u4', 'google', 'bats']
model = ['gpt2-xl', 'roberta-large']
df = df[df.model != 'bert-large-cased']
df = df.sort_values(by=['model'])

sns.set_theme(style="darkgrid")


def plot(df_, s, d):
    fig = plt.figure()
    fig.clear()
    if s == 'negative_permutation_aggregation':
        ax = sns.boxplot(x=s, y='accuracy', data=df_, hue='model', hue_order=model,
                         order=[r'val$_{}{}{}$'.format('{', n, '}') for n in range(1, 13)] + ['max', 'mean', 'min'])
    elif s == 'positive_permutation_aggregation':
        ax = sns.boxplot(x=s, y='accuracy', data=df_, hue='model', hue_order=model,
                         order=[r'val$_{}{}{}$'.format('{', n, '}') for n in range(1, 9)] + ['max', 'mean', 'min'])
    elif s == 'ppl_pmi_aggregation':
        ax = sns.boxplot(x=s, y='accuracy', data=df_, hue='model', hue_order=model,
                         order=[r'val$_{}{}{}$'.format('{', n, '}') for n in range(1, 3)] + ['max', 'mean', 'min'])
    else:
        ax = sns.boxplot(x=s, y='accuracy', data=df_, hue='model')
    handles, labels = ax.get_legend_handles_labels()
    print(labels)
    labels = [i.replace('roberta-large', 'RoBERTa').replace('gpt2-xl', 'GPT2') for i in labels]
    print(labels)
    # input()
    ax.legend(handles=handles, labels=labels)
    plt.setp(ax.get_legend().get_texts(), fontsize='15')

    # ax.set_xlabel(n, fontsize=15)
    ax.set_xlabel(None)
    ax.set_ylabel('Accuracy', fontsize=15)
    ax.tick_params(labelsize=15)
    fig = ax.get_figure()
    plt.tight_layout()
    plt.legend(loc='right')
    fig.savefig('./experiments_results/summary/main2_figure/box.{}.{}.png'.format(d, s))
    plt.close()


for s_tmp in ['positive_permutation_aggregation', 'negative_permutation_aggregation', 'ppl_pmi_aggregation']:
    plot(df, s_tmp, 'all')
    for data_ in data:
        df_tmp = df[df.data == data_]
        plot(df_tmp, s_tmp, data_)

