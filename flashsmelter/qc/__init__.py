"""化验取样与放行质控子系统（QC）。

主控台管的是炉子，这里管的是「料」：按批次安排取样点、录入化验单后自动判定，
不合格自动隔离卡住不放，复检与让步必须留审批记录，放行时锁定所依据的每一份
化验单，事后可以按批次一路追到原始报告与照片。

域内只有一套状态判定规则（``service.QualityService``），网页控制台与 CLI 都
经由同一个动作注册表调用，避免出现「页面能放、接口也能绕过去放」的两套口径。
"""

from .service import QualityService, build_service
from .settings import QcSettings
from .store import QcStore

__all__ = ["QualityService", "QcSettings", "QcStore", "build_service"]
