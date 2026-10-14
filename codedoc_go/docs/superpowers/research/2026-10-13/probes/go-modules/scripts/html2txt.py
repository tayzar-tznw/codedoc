import sys, html.parser, re
class P(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.out=[]; self.skip=0
    def handle_starttag(self, t, a):
        if t in ('script','style'): self.skip+=1
        if t in ('p','div','h1','h2','h3','h4','li','pre','tr','br','dt','dd'): self.out.append('\n')
        if t in ('h1','h2','h3','h4'):
            d=dict(a); 
            if 'id' in d: self.out.append('[#%s] ' % d['id'])
    def handle_endtag(self, t):
        if t in ('script','style'): self.skip-=1
    def handle_data(self, d):
        if not self.skip: self.out.append(d)
for f in sys.argv[1:]:
    p=P(); p.feed(open(f,encoding='utf-8').read())
    txt=''.join(p.out); txt=re.sub(r'\n\s*\n+', '\n\n', txt)
    open(f.rsplit('.',1)[0]+'.txt','w',encoding='utf-8').write(txt)
