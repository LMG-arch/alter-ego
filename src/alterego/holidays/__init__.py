"""随包的节日数据，以及它的**唯一**读取入口。

为什么数据在这个目录、读取代码也在这个目录：
和 `storage/sqlite/migrations/` + `migrator.py` 同一约定——数据与它的读取器放一起。
也正因为如此，`2026.toml` **不能**由 `domain/` 读：领域层做 IO 是红线 2，
`domain/calendar.py` 只提供吃字符串的 `HolidayCalendar.from_toml(text)`。

为什么查表而不是算：
节日的**日期**是天文事实（农历 / 节气），放假的**天数**是法条，
而「哪一天补班」是每年一发的行政公告——**行政决定推不出来**。
为了七个日期引一个农历库不划算（P5），查表反而更准，也能逐年刷新、被用户覆盖。

文件命名 `<年份>.toml`，一年一份，缺一年的正确表现是「那一年不知道有节」，
而不是让程序起不来（见 `load_year`）。

设计依据: `docs/design/12-calendar-and-conversation.md` § 1–2。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from alterego.domain.calendar import MAX_YEAR, MIN_YEAR, HolidayCalendar


__all__ = [
    "DATA_DIR",
    "available_years",
    "load_calendar",
    "load_year",
    "path_for_year",
]

DATA_DIR: Final[Path] = Path(__file__).resolve().parent
"""数据目录。跟着包走，所以 `pip install` 之后也在（见 pyproject 的 wheel 配置）。"""

logger = logging.getLogger(__name__)


def path_for_year(year: int) -> Path:
    """某一年数据文件的位置。

    只算路径、不判断存在——「文件在哪」和「文件在不在」是两件事，
    混在一起会让调用方分不清「路径算错了」和「今年还没写数据」。
    """
    if not MIN_YEAR <= year <= MAX_YEAR:
        msg = f"年份必须在 {MIN_YEAR} ~ {MAX_YEAR} 之间，收到 {year}"
        raise ValueError(msg)
    return DATA_DIR / f"{year}.toml"


def available_years() -> tuple[int, ...]:
    """随包带了的年份，升序。

    不缓存：一年查几次、每次都看磁盘，是为了让「刚放进去的文件立刻生效」，
    比省下的那点 `stat` 值钱。
    """
    years = [int(path.stem) for path in DATA_DIR.glob("*.toml") if path.stem.isdigit()]
    return tuple(sorted(years))


def load_year(year: int) -> HolidayCalendar | None:
    """读某一年的数据；**文件不存在返回 `None`，不报错**。

    为什么容忍缺失：`confirmed = false` 是出厂状态，2026 年还没人写 2027 的文件
    完全是常态。缺一年的正确表现是「那一年不知道有什么节」，
    而不是让整个程序起不来——「不知道」本来就是合法状态。

    但**文件存在却读不出来是另一回事**：那是有人写坏了（打错一个日期、
    少一个引号），必须让它炸。静默跳过的后果是那个节日从这一年里凭空消失，
    而且没有任何报错——比启动失败难查得多。

    缺失只记 `debug`。随包只带一两年，而 `load_calendar` 每次都要探三个年份，
    警告级别的结果是**每一条 CLI 命令开头都先吐两行「没有 2025/2027 年的数据」**——
    哨兵叫多了就不叫哨兵了。真要判断「这一年有没有」，看 `covers_year()`。
    """
    path = path_for_year(year)
    if not path.is_file():
        logger.debug("没有 %d 年的节日数据（%s 不存在）", year, path)
        return None
    return HolidayCalendar.from_toml(path.read_text(encoding="utf-8"), source=path.name)


def load_calendar(year: int) -> HolidayCalendar | None:
    """读 `year`，**顺带读它前后各一年并合并**。

    为什么不只读需要的那一年：12 月 29 日要看的是 1 月 1 日。
    跨年视野只能靠多读一份文件实现，而这正是「提前几天就知道」在年末的关键路径。

    三年全缺时返回 `None`（调用方走「没有节日数据」的分支）；
    只缺一部分就返回合并后的那份，`covers_year()` 会如实回答哪些年答不了。
    """
    found: list[HolidayCalendar] = []
    for offset in (-1, 0, 1):
        calendar = load_year(year + offset)
        if calendar is not None:
            found.append(calendar)

    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return HolidayCalendar.merge(*found)
