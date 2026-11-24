import re
import json

with open("luma_bundle.js", "r", encoding="utf-8") as f:
    js = f.read()

# Let's search for perfume product definitions
pattern = r'\{[^{}]*id:[^{}]*name:[^{}]*price:[^{}]*\}'
matches = re.findall(pattern, js)
print(f"Direct objects with id, name, price: {len(matches)}")
for m in matches[:10]:
    print("Match:", m)

# Let's search broader
print("\nSearching for fragrances / scents...")
for m in re.finditer(r'\{[^{}]*price:[0-9]+[^{}]*\}', js):
    print("Price snippet:", m.group()[:120])
