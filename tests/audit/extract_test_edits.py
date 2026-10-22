import re, subprocess, sys, json
def sh(*a): return subprocess.run(a, capture_output=True, text=True).stdout
files = sh('git','ls-files','tests/*.py','tests/*.js','tests/golden/*').split()
pat = re.compile(r'assert|check\(|expect|skip|self\.fail|raise |threshold|<=|>=|==|!=|timeout|sleep|time\.sleep|def test_|range\(|\bnot\b')
out = []
for f in files:
    commits = sh('git','log','--reverse','--format=%h|%s','main..HEAD','--',f).strip().splitlines()
    for c in commits[1:]:                      # first commit = creation; audit later edits only
        h, s = c.split('|',1)
        diff = sh('git','show','--format=','-U0',h,'--',f)
        rem = [l[1:].strip() for l in diff.splitlines() if l.startswith('-') and not l.startswith('---') and pat.search(l)]
        add = [l[1:].strip() for l in diff.splitlines() if l.startswith('+') and not l.startswith('+++')]
        out.append(dict(file=f, commit=h, subject=s, removed=rem, added_n=len(add)))
json.dump(out, open('/tmp/test-edits.json','w'), indent=1)
tot = sum(len(o['removed']) for o in out)
print(len(out), 'later edits;', tot, 'removed assertion-like lines')
for o in sorted(out, key=lambda o:-len(o['removed']))[:25]:
    print(len(o['removed']), o['file'], o['commit'], o['subject'][:70])
