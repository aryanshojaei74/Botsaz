"""card_wallet: رسیدِ رسیده بعد از پایانِ درخواستِ شارژ (state بدونِ amount) KeyError نمی‌دهد (PATCH_RECEIPT_STATE_ALIVE_V1)."""
import asyncio
from types import SimpleNamespace

from tenant.gateways import card_wallet as cw


class FakeState:
    def __init__(self, data):
        self.data = dict(data)
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, *_a, **_k):
        pass

    async def clear(self):
        self.cleared = True
        self.data = {}


class FakeMsg:
    def __init__(self):
        self.answers = []
        self.photo = [SimpleNamespace(file_id="f", file_unique_id="u")]
        self.text = "کد پیگیری 12345"

    async def answer(self, text, **_k):
        self.answers.append(text)

    async def answer_photo(self, *_a, **_k):
        self.answers.append("photo")


def test_receipt_without_amount_clears_state_and_informs_user():
    st, m = FakeState({"file_id": "x"}), FakeMsg()
    asyncio.run(cw.w_charge_receipt(m, st))
    assert st.cleared and len(m.answers) == 1 and "فعال نیست" in m.answers[0]


def test_text_receipt_without_amount_clears_state_and_informs_user():
    st, m = FakeState({}), FakeMsg()
    asyncio.run(cw.w_charge_receipt_text(m, st))
    assert st.cleared and "فعال نیست" in m.answers[0]


def test_receipt_with_amount_still_proceeds():
    st, m = FakeState({"amount": 50000}), FakeMsg()
    asyncio.run(cw.w_charge_receipt(m, st))
    assert not st.cleared and m.answers == ["photo"] and st.data["file_id"] == "f"
