import re, glob
tab = set(re.findall(r'^\| (\d+) \|', open('draft/numerals.md').read(), re.M))
fig = set()
for f in sorted(glob.glob('figures/*.md')):
    t = open(f).read().split('## Numerals shown on this figure')[1]
    n = set(re.findall(r'^- (\d+)', t, re.M))
    print(f, sorted(n, key=int))
    fig |= n
print('table not in figs', sorted(tab - fig, key=int))
print('figs not in table', sorted(fig - tab, key=int))
for f in sorted(glob.glob('figures/*.md')):
    body, nums = open(f).read().split('## Numerals shown on this figure')
    listed = set(re.findall(r'^- (\d+)', nums, re.M))
    used = set(re.findall(r'\b(\d{2})\b', body))
    print(f, 'in body not listed:', sorted(used - listed - {'20', '30'} if False else used - listed, key=int))
