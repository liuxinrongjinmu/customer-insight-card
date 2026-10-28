"""回写闭环：把人的修正变成下次不再犯的规则。

两个模块，职责不重叠：

- `corrections` 只负责「记下来」：从前后差分推出修正，追加写 JSONL。
- `lessons` 只负责「让它生效」：原文黑名单、字段改判、提示词补丁建议。

分开放的原因：记录一定会越来越多，而生效方式会变。绑在一起，
将来想换一种生效方式（比如从黑名单换成打分降权）就得连记录格式一起改。
"""

from .corrections import (
    LESSON_REASONS,
    REASON_CN,
    REASONS,
    Correction,
    append,
    counts,
    dedupe,
    diff,
    load,
)
from .lessons import (
    FIELD_OVERRIDE_MIN,
    Blacklist,
    Lesson,
    apply_blacklist,
    apply_overrides,
    block_note,
    lessons_from,
    suggest_patch,
)

__all__ = [
    "LESSON_REASONS", "REASON_CN", "REASONS", "Correction",
    "append", "counts", "dedupe", "diff", "load",
    "FIELD_OVERRIDE_MIN", "Blacklist", "Lesson",
    "apply_blacklist", "apply_overrides", "block_note", "lessons_from", "suggest_patch",
]
