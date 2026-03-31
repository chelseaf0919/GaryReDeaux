with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find where the HTML string starts and add raw prefix
# The string uses single quotes style
content = content.replace("HTML = '", "HTML = r'", 1)

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Done!")