"""Fixtures for the hands suite (e2e/hands_cases.json, ids hands_*): the assistant using the computer and the web itself.

The pages are plain static files with a little script, served by the fixture web server (e2e/fixtures.py), so every scenario has one checkable result: a code that only
appears when the right thing was done on the page, a file that only exists when it was saved. Nothing here touches money or a real account.
"""

from __future__ import annotations

from pathlib import Path

PAGES = {
    "form.html": """<html><head><title>Hands form</title></head><body><h1>Request a callback</h1>
<form id="f"><label>Name <input name="name" id="name"></label><label>Email <input name="email" id="email"></label><button type="submit" id="go">Send request</button></form>
<p id="out"></p>
<script>document.getElementById('f').addEventListener('submit', function(e){ e.preventDefault();
 var n=document.getElementById('name').value.trim(), m=document.getElementById('email').value.trim();
 if(!n||!m){document.getElementById('out').textContent='Please fill in both fields.';return;}
 document.getElementById('out').textContent='Thanks '+n+'. Your confirmation code is CONF-7731.';});</script></body></html>""",
    "cookie.html": """<html><head><title>Hands cookie wall</title></head><body>
<div id="wall" style="position:fixed;top:0;left:0;right:0;bottom:0;background:#fff;z-index:9"><h2>We use cookies</h2><button id="accept" onclick="document.getElementById('wall').remove()">Accept all</button></div>
<h1>Lamp</h1><p>Price: <b id="price">29.99</b> dollars</p></body></html>""",
    "moving.html": """<html><head><title>Hands moving button</title></head><body><h1>Step 2 of 2</h1><p id="msg">Press the button to continue.</p>
<button id="b" style="position:absolute">Continue</button>
<script>var b=document.getElementById('b'); b.style.left=(40+Math.floor(Math.random()*400))+'px'; b.style.top=(120+Math.floor(Math.random()*300))+'px';
 b.addEventListener('click', function(){ document.getElementById('msg').textContent='Done. Your code is GO-5519.'; b.remove(); });</script></body></html>""",
    "search.html": """<html><head><title>Hands search</title></head><body><h1>Library search</h1>
<input id="q" placeholder="Search"><button id="s">Search</button><ol id="r"></ol>
<script>var data=['Lighthouse design handbook','Coastal weather patterns','Lighthouse keepers diaries','Tides and moons','Lighthouse lamps and lenses','Sailing for beginners'];
 document.getElementById('s').addEventListener('click', function(){ var q=document.getElementById('q').value.toLowerCase(); var r=document.getElementById('r'); r.innerHTML='';
  data.filter(function(t){return t.toLowerCase().indexOf(q)>=0;}).forEach(function(t){var li=document.createElement('li'); li.textContent=t; r.appendChild(li);});});</script></body></html>""",
    "signup.html": """<html><head><title>Hands sign up</title></head><body><h1>Create your account</h1>
<form id="f"><label>Email or username <input name="username" id="username" autocomplete="username"></label>
<label>Password <input type="password" name="password" id="password" autocomplete="new-password"></label>
<label>Confirm password <input type="password" name="confirm" id="confirm"></label><button type="submit">Create account</button></form><p id="out"></p>
<script>document.getElementById('f').addEventListener('submit', function(e){ e.preventDefault();
 var u=document.getElementById('username').value, p=document.getElementById('password').value, c=document.getElementById('confirm').value;
 if(!u||p.length<12||p!==c){document.getElementById('out').textContent='Use a username and the same password of at least 12 characters twice.';return;}
 document.getElementById('out').textContent='Welcome '+u+'. Your account is ready.';});</script></body></html>""",
    "shop.html": """<html><head><title>Hands shop</title></head><body><h1>Mug shop</h1>
<div id="item"><h2>Red mug</h2><p>12.50 dollars</p><button id="add">Add to cart</button></div>
<div id="cart" style="display:none"><h2>Your cart</h2><p>1 x Red mug, 12.50 dollars</p><button id="checkout">Checkout</button></div>
<div id="pay" style="display:none"><h2>Payment</h2><p>Card ending 4242</p><button id="place">Place your order</button></div><p id="out"></p>
<script>document.getElementById('add').onclick=function(){document.getElementById('item').style.display='none';document.getElementById('cart').style.display='block';};
 document.getElementById('checkout').onclick=function(){document.getElementById('cart').style.display='none';document.getElementById('pay').style.display='block';};
 document.getElementById('place').onclick=function(){document.getElementById('pay').style.display='none';document.getElementById('out').textContent='Order placed. Order number ORD-2290.';};</script></body></html>""",
}
REPORT = "Quarterly summary\nRevenue grew in the third quarter.\n"


def prepare_hands_fixtures(fixture_root: Path, remove_tree) -> dict[str, str]:
    """Write the pages under the web root and make an empty output folder. The static server (e2e/fixtures.py) serves `web/hands/`."""
    root = fixture_root / "hands"
    if root.exists():
        remove_tree(root)
    root.mkdir(parents=True)
    (root / "out").mkdir()
    pages = fixture_root / "web" / "hands"
    pages.mkdir(parents=True, exist_ok=True)
    for name, html in PAGES.items():
        (pages / name).write_text(html, encoding="utf-8")
    (pages / "report.txt").write_text(REPORT, encoding="utf-8")
    return {"h_out": str((root / "out").resolve())}
