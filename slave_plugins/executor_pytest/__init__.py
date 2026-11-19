"""pytest 执行端（从插件）：材料组装 / 脚本分析 / 用例解析 / 测试执行。

设计见同目录 ``DESIGN.md``；主从经 comm 协作，契约住
``common/test_contracts.py``，族内私有 topic（``pytest.*``）本包自管。
"""
