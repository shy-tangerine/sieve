from lxml import html

from sieve.adaptive import _visible_text, relocate


def test_visible_text_counts_nested_text_once():
    element = html.fromstring("<div>before <span>nested</span> after</div>")

    assert _visible_text(element) == "before nested after"


def test_relocate_finds_replacement_after_stale_xpath():
    root = html.fromstring('<main><article id="replacement"><h2>Product X price 10 dollars</h2>'
                           '<p>Ships today from local stock</p></article>'
                           '<aside>Weather forecast and sports results</aside></main>')
    found = relocate(root, "//button[@data-id='old-product']",
                     "Product X price 10 dollars Ships today from local stock")
    assert found is not None and found.get("id") == "replacement"


def test_relocate_returns_none_below_confidence():
    root = html.fromstring("<main><p>Weather forecast</p><aside>Sports results</aside></main>")
    assert relocate(root, ".missing-product", "Quantum orchid subscription plan") is None


def test_relocate_prefers_structural_evidence_for_equal_visible_text():
    root = html.fromstring('<main><p>Buy this item</p>'
                           '<button aria-label="Buy">Buy this item</button></main>')
    found = relocate(root, ".old", "button: Buy this item")
    assert found is not None and found.tag == "button"
    root = html.fromstring('<main><aside>Weather sports</aside><a>Buy</a><button>Buy</button></main>')
    found = relocate(root, ".old", "button Buy")
    assert found is not None and found.tag == "button"


def test_relocate_candidate_scan_and_shared_work_are_bounded(monkeypatch):
    import sieve.adaptive as adaptive
    from sieve.dom_budget import DomWorkBudget

    root = html.fromstring('<main><p>Weather</p><p>Sports</p><p>Target orchid</p></main>')
    monkeypatch.setattr(adaptive, "_MAX_CANDIDATE_NODES", 2)
    seen = []
    score = adaptive._candidate_score

    def observed(*args):
        seen.append(args[-1])
        return score(*args)

    monkeypatch.setattr(adaptive, "_candidate_score", observed)
    relocate(root, ".old", "Target orchid")
    assert len(seen) == 2
    assert all(node.text != "Target orchid" for node in seen)
    budget = DomWorkBudget(total_nodes=1)
    assert relocate(root, ".old", "Target orchid", work_budget=budget) is None
    assert budget.exhausted and budget.used == 1
