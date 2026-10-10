import json
import random

from typer.testing import CliRunner

from route_map.cli import app
from route_map.inference import infer
from route_map.models import Config
from route_map.normalization import normalize

INVENTORY = "fixtures/route-map/shop-inventory.txt"


def run(urls, **config):
    return infer([(u, "inventory") for u in urls], Config(**config), seed="https://shop.test/")


def routes(result):
    return {r.pattern: r for r in result.routes}


def test_a_thousand_products_are_one_family_with_a_few_examples():
    result = run([f"https://shop.test/product/item-{n}" for n in range(1000)] + ["https://shop.test/about"])
    product = routes(result)["/product/{segment_2}"]
    assert product.status == "provisional" and product.discovered_url_count == 1000
    assert len(product.examples) == 3 and len(product.sample_ids) == 5
    assert len(result.routes) == 2


def test_root_pages_stay_individual():
    result = run(["https://shop.test/about", "https://shop.test/contact", "https://shop.test/privacy"])
    assert {r.pattern: r.status for r in result.routes} == {"/about": "individual", "/contact": "individual", "/privacy": "individual"}


def test_blog_posts_and_blog_categories_are_different_families():
    result = run([f"https://shop.test/blog/post-{n}" for n in range(3)] + [f"https://shop.test/blog/category/c{n}" for n in range(3)])
    assert set(routes(result)) == {"/blog/{segment_2}", "/blog/category/{segment_3}"}


def test_query_routes_keep_their_path_and_keys():
    result = run([f"https://shop.test/index.php?id={n}" for n in range(3)] + ["https://shop.test/index.php"])
    assert set(routes(result)) == {"/index.php?id={id}", "/index.php"}
    assert routes(result)["/index.php?id={id}"].parameters[0].type == "integer"


def test_tracking_parameters_are_removed_and_other_query_details_kept():
    n = normalize("HTTPS://Shop.Test:443/A%2Fb/?utm_source=x&b=&a=1&a=2#top", tracking=Config().tracking_parameters)
    assert n["crawl_url"] == "https://shop.test/A%2Fb/?b=&a=1&a=2"  # case, %2F, slash, blank value, repeats
    assert n["path_segments"] == ["A%2Fb"] and n["trailing_slash"]


def test_slash_and_case_differences_are_not_merged():
    result = run(["https://shop.test/Page", "https://shop.test/page", "https://shop.test/page/"])
    assert len(result.urls) == 3


def test_same_url_seen_twice_is_one_record_with_an_alias():
    result = run(["https://shop.test/a?utm_source=x", "https://shop.test/a", "http://shop.test/a"])
    assert len(result.urls) == 1 and len(result.urls[0].aliases) == 2


def test_locale_prefixes_stay_separate_routes():
    urls = [f"https://shop.test/{lang}/product/p{n}" for lang in ("en", "fr", "de") for n in range(4)]
    assert set(routes(run(urls))) == {"/en/product/{segment_3}", "/fr/product/{segment_3}", "/de/product/{segment_3}"}


def test_extension_stays_in_the_pattern():
    result = run([f"https://shop.test/p/item-{n}.html" for n in range(3)])
    assert set(routes(result)) == {"/p/{segment_2}.html"}


def test_two_values_are_not_enough_for_a_family():
    result = run(["https://shop.test/product/a", "https://shop.test/product/b"])
    assert all(r.status == "individual" for r in result.routes)


def test_result_does_not_depend_on_input_order():
    urls = [line for line in open(INVENTORY).read().splitlines() if line and not line.startswith("#")]
    first = run(urls).model_dump_json()
    random.Random(7).shuffle(urls)
    assert run(urls).model_dump_json() == first


def test_every_url_has_exactly_one_route_and_counts_reconcile():
    urls = [line for line in open(INVENTORY).read().splitlines() if line and not line.startswith("#")]
    result = run(urls)
    ids = {r.id for r in result.routes}
    assert all(u.route_id in ids for u in result.urls)
    assert sum(r.discovered_url_count for r in result.routes) == len(result.urls)
    assert set(result.crawl_report.rejected_urls) == {"https://other.test/product/elsewhere", "mailto:hello@shop.test"}


def test_hash_router_urls_are_reported_as_a_limitation():
    result = run(["https://shop.test/#/product/1"])
    assert result.urls[0].hash_route and result.crawl_report.limitations


def test_cli_maps_an_inventory(tmp_path):
    out = tmp_path / "map.json"
    result = CliRunner().invoke(app, ["https://shop.test/", "--inventory", INVENTORY, "-o", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["schema_version"] == "1"
    patterns = {r["pattern"] for r in data["routes"]}
    assert {"/product/{segment_2}", "/blog/{segment_2}", "/blog/category/{segment_3}", "/search?q={q}", "/about"} <= patterns


def test_cli_refuses_a_seed_that_is_not_http(tmp_path):
    result = CliRunner().invoke(app, ["ftp://shop.test/", "-o", str(tmp_path / "m.json")])
    assert result.exit_code != 0 and not (tmp_path / "m.json").exists()


def test_cli_rejects_an_invalid_budget(tmp_path):
    result = CliRunner().invoke(app, ["https://shop.test/", "--inventory", INVENTORY, "--max-html-pages", "0"])
    assert result.exit_code != 0 and "max-html-pages" in result.output
