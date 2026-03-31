with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if 'DOCTYPE' in line or ('HTML' in line and '"""' in line):
        print(f'Line {i+1}: {repr(line[:80])}')