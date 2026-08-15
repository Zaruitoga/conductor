"""
tests/test_surfaces.py — The three pages this process serves, each reachable.

There is exactly one thing to get wrong here and it is silent: the root mount is
a **catch-all**, so a page mounted after it is not 404 — it is served the control
panel's `index.html`, with a 200 and the right content type.  The browser then
fails on a script the panel's HTML never asked for, several layers away from the
line that caused it.  `api/app.py` carries the warning in a comment; this module
is what makes it fail loudly instead.

`TestClient(app)` is instantiated **without** a `with` block, for the reason
`test_video.py` gives at length: the context manager runs the lifespan, which
boots the UDP socket, the WS server, the ESP configurator and the processing
loop.  Serving a static file involves none of it.

Each surface is identified by the module it loads, not by its title: a title is
prose that will be reworded, while the entry point is what the mount exists to
deliver, and it is precisely what a swallowed page fails to deliver.
"""

from starlette.testclient import TestClient

from api.app import app

# surface → the script its index.html loads. Distinct on purpose: the assertion
# is that each page is *itself*, and the failure mode is being handed another.
SURFACES = {
    "/":       "/js/main.js",
    "/viz/":   "viz.js",
    "/align/": "align.js",
}


def test_each_surface_serves_its_own_page():
    client = TestClient(app)
    for path, entry in SURFACES.items():
        res = client.get(path)
        assert res.status_code == 200, f"{path} → {res.status_code}"
        assert "text/html" in res.headers["content-type"], path
        assert entry in res.text, f"{path} does not load {entry}"


def test_a_sub_page_is_not_swallowed_by_the_catch_all():
    """
    The trap, stated directly: `/align/` must not be handed the panel.

    Checking the entry point above already catches it, but only as "the page is
    wrong"; this says which page it is, which is the whole diagnosis.
    """
    client = TestClient(app)
    panel = client.get("/").text
    for path in ("/viz/", "/align/"):
        assert client.get(path).text != panel, f"{path} was served the panel"


def test_the_static_assets_of_each_surface_are_reachable():
    """A mounted page whose own modules 404 renders blank, with a 200 on the HTML."""
    client = TestClient(app)
    for path in ("/js/main.js", "/viz/viz.js", "/align/align.js",
                 "/align/curve.js", "/align/video.js", "/align/align.css"):
        assert client.get(path).status_code == 200, path


def main():
    test_each_surface_serves_its_own_page()
    test_a_sub_page_is_not_swallowed_by_the_catch_all()
    test_the_static_assets_of_each_surface_are_reachable()
    print("  surfaces: ok")


if __name__ == "__main__":
    main()
