with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines[355:370], start=356):
    print(f'Line {i}: {repr(line[:80])}')