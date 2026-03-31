"""fix_html_string.py — Run this once to fix the HTML string in main.py"""

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the HTML string and make it a raw string
old = 'HTML = """<!DOCTYPE html>'
new = 'HTML = r"""<!DOCTYPE html>'

if old in content:
    content = content.replace(old, new)
    with open('main.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Done! HTML string is now a raw string.")
elif new in content:
    print("Already fixed! HTML string is already a raw string.")
else:
    print("Could not find HTML string — check main.py manually.")