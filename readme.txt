秋窗客流分析账本
运行环境：Python 3.11，已按 requirements.txt 预装依赖。
启动：python -m autumn_ledger serve --database autumn-ledger.sqlite3
接口说明：服务启动后访问 /docs。
合成业务演练：python -B -m autumn_ledger demo
测试：python -B -m unittest discover -s tests -v
构建：python -B -m autumn_ledger.check_source
数据完整性：python -B -m autumn_ledger check --database autumn-ledger.sqlite3
金额单位为人民币分；订单修订为全量快照，退款为累计值。
出游日期是供应商给出的目的地本地日期；事件时间必须含时区。
取消不计出游人数，未退收入仍计入净额。缺报不等于零需求。
本地演练使用虚构数据，分析仅描述所选来源样本。
经营范围：PUT /operating-scopes/{id} 登记来源在哪些城市、哪些日期应报数（applicable），
  GET /operating-scopes/{id}/history 回看修订；未声明范围视为尚未确认，不自动当作不经营。
  分析请求加 scope_mode=declared 启用新口径：范围外标不适用、不进入暴露分母；范围内 complete/partial/missing
  含义不变；范围外订单列为冲突并阻止审核发布；同一格规则冲突且无法唯一解释时拒绝登记。
  不含 scope_mode 的旧请求沿用原全组合覆盖语义，旧报告可按 revision 重现。
