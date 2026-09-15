"""``alterego.birthdays``（生日文件的读写）的测试。

这一层只做两件事：把文件读成一本书、把一本书写成文件。
**规则一条都不在这里**——月份合不合法、同一天要不要合并，都在
``alterego.domain.birthday`` 里，那边有 ``test_domain_birthday.py``。
这里要证的是接缝：字节进出之后还是同一件事。

所以下面最重的两条是：

* ``test_a_missing_file_is_an_empty_book``：没有文件是**默认状态**而不是错误。
  写死成「读不到就抛」的话，一个刚装好的实例连 ``calendar today`` 都跑不起来。
* ``test_the_bytes_are_lf_only``：不加 ``newline="\\n"`` 的话，在 Windows 上
  写出来的是 CRLF，而仓库里全是 LF——那份文件一旦进版本库，之后每次改动
  都会显示成整个文件被重写。

这里用的都是 ``tmp_path``，绝不碰开发机上真实的 ``data/birthdays.toml``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alterego.birthdays import FILE_NAME, default_path, load_book, render, save_book
from alterego.domain.birthday import DEFAULT_LEAD_DAYS, Birthday, BirthdayBook


def _birthday(**overrides: object) -> Birthday:
    subject = overrides.get("subject", "user")
    fields: dict[str, object] = {
        "subject": subject,
        "name": "小明",
        "month": 6,
        "day": 3,
        "lead_days": DEFAULT_LEAD_DAYS[subject],  # type: ignore[index]
        "aftermath_days": 3,
    }
    fields.update(overrides)
    return Birthday(**fields)  # type: ignore[arg-type]


class TestDefaultPath:
    def test_it_sits_in_the_data_directory(self) -> None:
        assert default_path(Path("data")) == Path("data") / FILE_NAME

    def test_the_file_name_is_a_constant(self) -> None:
        # 文件名跟着数据目录走，是 `Config.birthdays_path` 也用的那个名字。
        # 两处各写一个字符串必然分叉，分叉的表现是「CLI 读 A、启动读 B」。
        assert FILE_NAME == "birthdays.toml"


class TestLoadBook:
    def test_a_missing_file_is_an_empty_book(self, tmp_path: Path) -> None:
        assert len(load_book(tmp_path / FILE_NAME)) == 0

    def test_a_missing_directory_is_also_fine(self, tmp_path: Path) -> None:
        assert len(load_book(tmp_path / "nowhere" / FILE_NAME)) == 0

    def test_a_real_file_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / FILE_NAME
        path.write_text(
            '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\n',
            encoding="utf-8",
        )
        book = load_book(path)
        assert len(book) == 1
        assert book.birthdays[0].month_day == "06-03"

    def test_a_broken_file_is_rejected(self, tmp_path: Path) -> None:
        # 静默跳过的后果是「某个人的生日凭空消失」，而那天看起来完全正常——
        # 比启动时炸一次难查得多。
        path = tmp_path / FILE_NAME
        path.write_text('[[birthday]]\nsubject = "user"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="缺少必填字段"):
            load_book(path)

    def test_the_error_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / FILE_NAME
        path.write_text("[oops\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"birthdays\.toml"):
            load_book(path)

    def test_chinese_is_read_back(self, tmp_path: Path) -> None:
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook(birthdays=(_birthday(name="妈妈"),)))
        assert load_book(path).birthdays[0].name == "妈妈"


class TestSaveBook:
    def test_it_creates_the_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / FILE_NAME
        save_book(path, BirthdayBook())
        assert path.is_file()

    def test_the_bytes_are_lf_only(self, tmp_path: Path) -> None:
        # Windows 上的 `write_text` 默认会把 `\n` 翻成 `\r\n`。
        # 这份文件一旦提交，之后每次改动都会显示成整个文件被重写。
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook(birthdays=(_birthday(),)))
        assert b"\r\n" not in path.read_bytes()

    def test_it_does_not_append(self, tmp_path: Path) -> None:
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook(birthdays=(_birthday(),)))
        save_book(path, BirthdayBook(birthdays=(_birthday(subject="self", name="自己"),)))
        assert len(load_book(path)) == 1

    def test_it_writes_utf8_so_chinese_survives(self, tmp_path: Path) -> None:
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook(birthdays=(_birthday(note="身份证上写的是六月三号"),)))
        assert "六月三号" in path.read_text(encoding="utf-8")


class TestRender:
    def test_an_empty_book_still_has_the_header(self) -> None:
        # 文件的第一行是「怎么改这份文件」的说明。没有表也能看懂该怎么写。
        text = render(BirthdayBook())
        assert text.startswith("#")
        assert "alterego birthday add" in text

    def test_the_header_explains_the_leap_day(self) -> None:
        assert "2 月 29 日" in render(BirthdayBook())


class TestRoundTrip:
    def test_an_empty_book_survives(self, tmp_path: Path) -> None:
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook())
        assert load_book(path) == BirthdayBook()

    def test_a_full_record_survives(self, tmp_path: Path) -> None:
        original = BirthdayBook(
            note="随手记的",
            birthdays=(
                _birthday(
                    note="跟本人确认过",
                    prep_activities=("挑礼物", "订蛋糕"),
                    during_activities=("说生日快乐",),
                    aftermath_activities=("问问礼物好不好用",),
                ),
            ),
        )
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path) == original

    def test_an_npc_record_survives(self, tmp_path: Path) -> None:
        original = BirthdayBook(
            birthdays=(_birthday(subject="npc", name="张三", npc_id="npc-7", lead_days=20),)
        )
        path = tmp_path / FILE_NAME
        save_book(path, original)
        back = load_book(path)
        assert back == original
        assert back.birthdays[0].key == "npc-7"

    def test_the_leap_day_survives(self, tmp_path: Path) -> None:
        original = BirthdayBook(birthdays=(_birthday(month=2, day=29),))
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path).birthdays[0].month_day == "02-29"

    def test_an_unverified_record_stays_unverified(self, tmp_path: Path) -> None:
        original = BirthdayBook(birthdays=(_birthday(verified=False),))
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path).birthdays[0].verified is False

    def test_a_custom_lead_days_survives(self, tmp_path: Path) -> None:
        original = BirthdayBook(birthdays=(_birthday(lead_days=20),))
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path).birthdays[0].lead_days == 20

    def test_the_second_write_is_byte_identical(self, tmp_path: Path) -> None:
        # 写→读→写 必须收敛。不收敛的话每次 `birthday add` 都会改动整个文件，
        # `git diff` 从此没有信息量。
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook(birthdays=(_birthday(),)))
        first = path.read_bytes()
        save_book(path, load_book(path))
        assert path.read_bytes() == first

    def test_a_name_with_a_quote_survives(self, tmp_path: Path) -> None:
        # 不转义的话会写出一个**读不回来**的文件，而错误在下一次读取时才出现——
        # 那时已经没人记得是谁写的了。
        original = BirthdayBook(birthdays=(_birthday(name='要说"生日快乐"的那个人'),))
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path) == original

    def test_a_name_with_a_backslash_survives(self, tmp_path: Path) -> None:
        original = BirthdayBook(birthdays=(_birthday(name="C:\\Users"),))
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path) == original

    def test_a_name_with_a_newline_survives(self, tmp_path: Path) -> None:
        original = BirthdayBook(birthdays=(_birthday(name="第一行\n第二行"),))
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path) == original

    def test_defaults_are_omitted_from_the_file(self, tmp_path: Path) -> None:
        # 写全会让这份文件看起来像一份配置文件，而它其实是几行事实。
        # 省略是安全的，因为省略的条件恰好就是「再读回来还是这个值」。
        path = tmp_path / FILE_NAME
        save_book(path, BirthdayBook(birthdays=(_birthday(subject="user"),)))
        # 只看数据行：抬头的注释本来就在讲 `lead_days` 是什么意思。
        data = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        assert data == ["[[birthday]]", 'subject = "user"', 'name = "小明"', "month = 6", "day = 3"]
        assert load_book(path).birthdays[0].lead_days == DEFAULT_LEAD_DAYS["user"]

    def test_two_people_on_one_day_survive(self, tmp_path: Path) -> None:
        original = BirthdayBook(
            birthdays=(
                _birthday(name="小明"),
                _birthday(subject="npc", name="爸爸", npc_id="npc-1"),
            )
        )
        path = tmp_path / FILE_NAME
        save_book(path, original)
        assert load_book(path) == original
