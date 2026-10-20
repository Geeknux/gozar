"""M6.5: file and image fields — multipart saves, keeping, replacing and clearing
files through Django's storage API, limits, image validation, inline images and
"save as new"."""

import io
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import encode_multipart

from bi_admin.models import ChangeLog
from shop.models import Product, ProductImage

API = "/gozar/api"
pytestmark = pytest.mark.django_db
BOUNDARY = "GozarTestBoundary"


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


def png(color="red", size=(4, 3), name="photo.png"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


def pdf(name="sheet.pdf", body=b"%PDF-1.4 demo"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def multipart(client, method, url, payload, **files):
    data = {"__payload__": json.dumps(payload), **files}
    if method == "post":
        return client.post(url, data)  # the test client encodes multipart by default
    return getattr(client, method)(
        url,
        encode_multipart(BOUNDARY, data),
        content_type=f"multipart/form-data; boundary={BOUNDARY}",
    )


@pytest.fixture
def product(db):
    return Product.objects.create(name="Lamp", category="home", price=100)


def test_meta_marks_file_and_image_fields(admin_client, product):
    meta = admin_client.get(f"{API}/meta/models/shop/product").json()
    fields = {f["name"]: f for f in meta["fields"]}
    assert fields["datasheet"]["kind"] == "file"
    assert meta["form"]["widgets"]["datasheet"] == {"type": "file"}
    assert "datasheet" not in meta["form"]["readonly_fields"]
    images = meta["form"]["inlines"][0]
    image = next(f for f in images["field_meta"] if f["name"] == "image")
    assert image["image"] is True and image["kind"] == "file"
    assert images["widgets"]["image"] == {"type": "image"}
    assert fields["datasheet"]["image"] is False


def test_upload_on_create_and_update_with_patch(admin_client, settings):
    resp = multipart(
        admin_client,
        "post",
        crud("shop.product"),
        {"name": "Kettle", "category": "home", "price": "250"},
        datasheet=pdf(),
    )
    assert resp.status_code == 201, resp.json()
    value = resp.json()["values"]["datasheet"]
    assert value["filename"] == "sheet.pdf"
    assert value["url"].startswith(settings.MEDIA_URL)
    product = Product.objects.get(name="Kettle")
    assert product.datasheet.name.startswith("products/datasheets/")
    assert product.datasheet.read() == b"%PDF-1.4 demo"
    first = product.datasheet.name

    # PATCH with multipart (Django only parses multipart for POST; the API handles it)
    resp = multipart(
        admin_client,
        "patch",
        crud("shop.product", product.pk),
        {"price": "300"},
        datasheet=pdf("v2.pdf", b"%PDF-1.4 v2"),
    )
    assert resp.status_code == 200, resp.json()
    product.refresh_from_db()
    assert product.price == 300 and product.datasheet.name != first
    assert product.datasheet.read() == b"%PDF-1.4 v2"
    entry = ChangeLog.objects.filter(model_label="shop.product", object_pk=str(product.pk)).first()
    assert set(entry.changes["fields"]) == {"price", "datasheet"}
    assert entry.changes["diff"]["datasheet"]["old"] == first


def test_keeping_and_clearing_a_file(admin_client, product):
    multipart(admin_client, "patch", crud("shop.product", product.pk), {}, datasheet=pdf())
    product.refresh_from_db()
    name = product.datasheet.name
    current = admin_client.get(crud("shop.product", product.pk)).json()["values"]["datasheet"]

    # sending the value back as it was read (the SPA does) keeps it; so does leaving it out
    for body in ({"datasheet": current, "name": "Lamp 2"}, {"name": "Lamp 3"}):
        resp = admin_client.patch(crud("shop.product", product.pk), body, "application/json")
        assert resp.status_code == 200, resp.json()
        product.refresh_from_db()
        assert product.datasheet.name == name

    resp = admin_client.patch(
        crud("shop.product", product.pk), {"datasheet": None}, "application/json"
    )
    assert resp.status_code == 200
    product.refresh_from_db()
    assert not product.datasheet
    assert resp.json()["values"]["datasheet"] is None


def test_a_required_file_cant_be_cleared_and_must_be_given(admin_client, product):
    url = crud("shop.product", product.pk)
    resp = multipart(
        admin_client,
        "patch",
        url,
        {"__inlines__": {"images": [{"values": {"caption": "front"}}]}},
    )
    assert resp.status_code == 400
    assert "image" in resp.json()["inline_errors"]["images"]["rows"]["0"]

    resp = multipart(
        admin_client,
        "patch",
        url,
        {"__inlines__": {"images": [{"values": {"caption": "front"}}]}},
        **{"images.0.image": png()},
    )
    assert resp.status_code == 200, resp.json()
    image = ProductImage.objects.get(product=product)
    assert image.caption == "front" and image.image.name.startswith("products/images/")

    resp = admin_client.patch(
        url,
        {"__inlines__": {"images": [{"pk": image.pk, "values": {"image": None}}]}},
        "application/json",
    )
    assert resp.status_code == 400
    assert resp.json()["inline_errors"]["images"]["rows"]["0"]["image"]
    image.refresh_from_db()
    assert image.image


def test_images_are_validated(admin_client, product):
    resp = multipart(
        admin_client,
        "patch",
        crud("shop.product", product.pk),
        {"__inlines__": {"images": [{"values": {"caption": "x"}}]}},
        **{"images.0.image": SimpleUploadedFile("fake.png", b"not an image")},
    )
    assert resp.status_code == 400
    assert resp.json()["inline_errors"]["images"]["rows"]["0"]["image"]
    assert not ProductImage.objects.exists()


def test_upload_size_limit(admin_client, product, settings):
    settings.GOZAR = {**settings.GOZAR, "UPLOAD_MAX_BYTES": 10}
    resp = multipart(admin_client, "patch", crud("shop.product", product.pk), {}, datasheet=pdf())
    assert resp.status_code == 400
    assert "too large" in resp.json()["errors"]["datasheet"][0]
    product.refresh_from_db()
    assert not product.datasheet


def test_upload_needs_change_permission(client, make_staff, product):
    client.force_login(make_staff("shop.view_product"))
    resp = multipart(client, "patch", crud("shop.product", product.pk), {}, datasheet=pdf())
    assert resp.status_code == 403
    product.refresh_from_db()
    assert not product.datasheet


def test_files_go_through_the_storage_api(admin_client, product, settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "other"), "base_url": "/files/"},
        },
    }
    # (the settings fixture's setting_changed signal resets default_storage)
    resp = multipart(admin_client, "patch", crud("shop.product", product.pk), {}, datasheet=pdf())
    assert resp.status_code == 200, resp.json()
    assert resp.json()["values"]["datasheet"]["url"].startswith("/files/")
    product.refresh_from_db()
    assert (tmp_path / "other" / product.datasheet.name).exists()


def test_save_as_new_copies_values_and_own_files_only(admin_client, product):
    url = crud("shop.product", product.pk)
    multipart(
        admin_client,
        "patch",
        url,
        {"__inlines__": {"images": [{"values": {"caption": "a"}}]}},
        datasheet=pdf(),
        **{"images.0.image": png()},
    )
    product.refresh_from_db()
    source = admin_client.get(url).json()
    image_row = source["inlines"]["images"]["rows"][0]
    body = {
        **{k: v for k, v in source["values"].items() if k in ("category", "price", "datasheet")},
        "name": "Lamp (copy)",
        "tags": [],
        "__save_as__": product.pk,
        "__inlines__": {"images": [{"pk": None, "values": image_row["values"]}]},
    }
    resp = admin_client.post(crud("shop.product"), body, "application/json")
    assert resp.status_code == 201, resp.json()
    copy = Product.objects.get(name="Lamp (copy)")
    assert copy.datasheet.name == product.datasheet.name
    assert copy.images.get().image.name == product.images.get().image.name
    assert ChangeLog.objects.filter(
        model_label="shop.product", object_pk=str(copy.pk), action="add"
    ).get().changes["copied_from"] == str(product.pk)

    # a file name that isn't the source object's is ignored (no reaching into storage)
    body = {
        "name": "Sneaky",
        "category": "home",
        "price": "1",
        "datasheet": {"name": "products/datasheets/someone-elses.pdf"},
        "__save_as__": product.pk,
    }
    resp = admin_client.post(crud("shop.product"), body, "application/json")
    assert resp.status_code == 201
    assert not Product.objects.get(name="Sneaky").datasheet


def test_save_as_requires_the_option(admin_client, seeded):
    from shop.models import City

    city = City.objects.first()
    resp = admin_client.post(
        crud("shop.city"),
        {"name": "Copy", "province": "X", "__save_as__": city.pk},
        "application/json",
    )
    assert resp.status_code == 400


def test_list_shows_image_columns(admin_client, product, monkeypatch):
    from shop.gozar import ProductOptions

    monkeypatch.setattr(ProductOptions, "list_display", ["name", "datasheet"])
    multipart(admin_client, "patch", crud("shop.product", product.pk), {}, datasheet=pdf())
    data = admin_client.get(crud("shop.product")).json()
    assert data["columns"][1]["kind"] == "file"
    assert data["rows"][0]["cells"][1]["filename"] == "sheet.pdf"
