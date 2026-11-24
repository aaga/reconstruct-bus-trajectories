"""Combined page: B41 Flatbush Avenue (introduces the method) + nine Bus Action Plan corridor sections."""
import sys, importlib.util
from pathlib import Path
HERE = Path(__file__).parent; FIG = HERE.parent
def mod(name, path):
    sys.path.insert(0, str(path.parent)); sp = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
sys.argv = [sys.argv[0], "ff"]
b41 = mod("b41build", FIG / "b41_ltd" / "build.py"); bap = mod("bapbuild", FIG / "bap_corridors" / "build.py")
T = open(FIG / "b41_ltd" / "template_v5.html").read()
head = T[T.index("<meta name"):T.index('<div class="wrap">')]; tail = T[T.index('<div id="tip"'):]
def sub(s, a, b, n=1):
    assert s.count(a) == n, (a, s.count(a)); return s.replace(a, b)
head = sub(head, "--c-ff: #a9a79f;", "--c-ff: #1baf7a;"); head = sub(head, "--c-oth: #1baf7a;", "--c-oth: #f0a01e;")
head = sub(head, "--c-ff: #77756e;", "--c-ff: #199e70;", 2); head = sub(head, "--c-oth: #199e70;", "--c-oth: #d98f1c;", 2)
head = sub(head, "--c-road: #5d5c58;", "--c-lanestroke: #a23a2c; --c-road: #5d5c58;"); head = sub(head, "--c-road: #3b3b38;", "--c-lanestroke: #e9a79c; --c-road: #3b3b38;", 2)
head = sub(head, ".t-stop { font: 10.5px var(--body); fill: var(--fg2); }", ".t-stop { font: 600 10.5px var(--body); fill: var(--c-stop); }")
head = sub(head, ".t-stop.b { font-weight: 600; fill: var(--fg); }", "")
head = sub(head, "</style>", """.t-in.dk, .t-in2.dk { fill: #14140f; }
path[fill="var(--c-oth)"] { fill-opacity: .34; }
path.hz[fill="var(--c-oth)"]:hover, path.hz[fill="var(--c-oth)"].on { filter: none; fill-opacity: .44; }
.tl { fill: #1d1d1b; stroke: #fff; stroke-width: 1; }
.sgn { fill: var(--bg); stroke: var(--rule); stroke-width: 1; } .t-x { font: 600 8.8px var(--body); letter-spacing: .05em; fill: var(--fg2); pointer-events: none; }
.key span { display: inline-flex; align-items: center; } .key svg { margin-right: 6px; flex: none; }
.k-stopname { font-style: normal; font-weight: 600; font-size: 11px; color: var(--c-stop); margin-right: 6px; } .k-stopname::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 1.5px; background: var(--c-stop); margin-right: 4px; }
.k-sgn { font-style: normal; font-weight: 600; font-size: 9px; letter-spacing: .05em; color: var(--fg2); background: var(--bg); border: 1px solid var(--rule); border-radius: 3px; padding: 1px 5px; }
.tag { display: inline-block; font-size: 12px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; color: var(--fg2); border: 1px solid var(--rule); border-radius: 999px; padding: 2px 10px; margin-right: 6px; }
.corr { border-top: 1px solid var(--rule); padding-top: 32px; } .k-plan { width: 18px; height: 8px; background: var(--c-lane); opacity: .55; outline: 1px dashed var(--c-lanestroke); border-radius: 2px; }
</style>""")
# dotted links start at the edge of the traffic-light symbol, on the side of the blob
tail = sub(tail, "line(layer, dot.dataset.x, dot.dataset.y, z.dataset.ax, z.dataset.ay);", "line(layer, dot.dataset.x, +dot.dataset.y + (+z.dataset.ay < +dot.dataset.y ? -11 : 11), z.dataset.ax, z.dataset.ay);")
KEY = ('<span><i class="k-line"></i>delay curve</span>'
       '<span><svg width="9" height="20" viewBox="0 0 9 21"><rect class="tl" x=".5" y=".5" width="8" height="20" rx="3"/><circle cx="4.5" cy="4.6" r="2" fill="#ff5a52"/><circle cx="4.5" cy="10.5" r="2" fill="#ffc233"/><circle cx="4.5" cy="16.4" r="2" fill="#35c96b"/></svg>traffic light, both directions</span>'
       '<span><i class="k-stopname">Church Av</i>bus stop</span><span><i class="k-sgn">CHURCH AV</i>&nbsp;cross street</span>')
secs, summ = bap.sections()
page = open(HERE / "body.html").read(); rep = dict(b41.rep); rep.update(KEY=KEY, SECTIONS="\n".join(secs), SUMMARY="".join(summ))
for k, v in rep.items(): page = page.replace("{{" + k + "}}", v)
import re; left = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", page))); assert not left, left
out = "<title>Where NYC Buses Lose Time</title>\n" + head + page + tail
open(HERE / "bus_delay.html", "w").write(out); print("written", len(out) // 1024, "KB")
