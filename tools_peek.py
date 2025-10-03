import re
from pathlib import Path

p = Path('logcat_rotate.py')
s = p.read_text(encoding='utf-8', errors='replace')
i = s.find('bug_dir = out_dir')
print('bug_dir occurrence at:', i)
print(s[max(0, i-140): i+240])

j = s.find('parser = argparse.ArgumentParser')
print('\n--- parser block ---')
print(s[j: j+800])

