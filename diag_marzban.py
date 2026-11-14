import asyncio
import sys

sys.path.insert(0, ".")

PANEL_URL = "https://wgmg.dreamdress.ir:8444/"
USERNAME = "gm2oz0r"
PASSWORD = "MT45EHGg3r"
INBOUND_TAGS_OVERRIDE = None


async def main():
    from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient, PanelError

    client = MarzbanPanelClient(
        PANEL_URL, USERNAME, PASSWORD,
        inbound_ids=INBOUND_TAGS_OVERRIDE, verify_ssl=False, timeout=15,
    )

    print("=== STEP 1: login ===")
    try:
        await client.login(force=True)
        print("OK: login succeeded")
    except PanelError as e:
        print("FAIL: login failed:")
        print(e)
        await client.close()
        return

    print("=== STEP 2: list_inbounds ===")
    try:
        inbounds = await client.list_inbounds()
        if not inbounds:
            print("WARNING: panel returned zero inbounds")
        else:
            print("OK: found " + str(len(inbounds)) + " inbounds:")
            for ib in inbounds:
                print("  tag=" + repr(ib.get("tag")) + " protocol=" + str(ib.get("protocol")))
    except PanelError as e:
        print("FAIL: list_inbounds failed:")
        print(e)
        await client.close()
        return

    print("=== STEP 3: active_inbound_ids (override=" + repr(INBOUND_TAGS_OVERRIDE) + ") ===")
    tags = await client.active_inbound_ids()
    print("target tags:", tags)

    print("=== STEP 4: create_account 1GB / 1 hour ===")
    test_email = "diag_test_1h"
    try:
        result = await client.create_account(test_email, gb=1, days=0, hours=1)
        print("OK: config created")
        print("sub_link:", result.get("sub_link"))
        print("uuid:", result.get("uuid"))
        print("cleaning up test user...")
        try:
            await client.delete_account(result.get("email", test_email))
            print("OK: test user deleted")
        except Exception as de:
            print("WARNING: could not delete test user, remove it manually:")
            print(de)
    except PanelError as e:
        print("FAIL: create_account failed:")
        print(e)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
