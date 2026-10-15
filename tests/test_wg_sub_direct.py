from tenant.bots.vpn.vpn_user import wg_files_from_sub_response

CONF = b"[Interface]\nPrivateKey = abc\nAddress = 10.0.0.2/32\nMTU = 1280\n\n[Peer]\nPublicKey = xyz\nEndpoint = h:51820\n"


def test_single_file_is_byte_identical_and_keeps_name():
    out = wg_files_from_sub_response(CONF, "Turkey.conf")
    assert out == [("Turkey.conf", CONF)]


def test_name_without_extension_and_missing_name():
    assert wg_files_from_sub_response(CONF, "Turkey")[0][0] == "Turkey.conf"
    assert wg_files_from_sub_response(CONF, None)[0][0] == "wireguard.conf"


def test_multi_blob_split_with_names():
    blob = b"# Name: DE\n" + CONF + b"\n# Name: TR\n" + CONF
    out = wg_files_from_sub_response(blob, None)
    assert [n for n, _ in out] == ["DE.conf", "TR.conf"]
    assert all(b"[Interface]" in d for _, d in out)


def test_base64_and_non_wg():
    import base64
    assert wg_files_from_sub_response(base64.b64encode(CONF), "a.conf")[0][1] == CONF
    assert wg_files_from_sub_response(b"vless://abc", "a.conf") == []
