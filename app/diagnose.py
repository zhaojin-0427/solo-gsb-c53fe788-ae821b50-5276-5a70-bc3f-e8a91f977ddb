"""现场电流偏差定位。

对一条**已保存的历史试算记录**，从其保存的组参数与基线接线重新构建
分段串曲线（绝不使用结果快照中那套稀疏采样做插值），在用户提交的实测
端电压测点上求理论电流，并比较两类假设：

1. 健康状态：全部组参数与记录一致；
2. 单组 Isc 退化：恰有一组短路电流降为某个**小于原 Isc 的正数、至多
   两位小数**的值（Voc 不变，接线不变）。

每个测点都满足 |实测电流 − 理论电流| ≤ 容差，该假设才算“可解释”。

拐点处由 ``_segment_at`` 精确选段（分段连续，不会因选段产生跳变）；
端电压达到某串 Voc 的支路电流为 0、对该串内各组的退化不敏感，
这些事实由 ``string_current`` / ``total_current`` 精确处理，网格
枚举无需在拐点上做任何插值。

---- 可行域结构与离散可行带两端搜索 ----

固定一个测点 (V, I_测)，仅让某组 g 的 Isc 候选 x 在 0.01 网格上变化：
该组所在串的理论电流 I_S(V; x) 关于 x **单调不减**（x 增大只可能把该
组从旁路拉回活动态、并抬升活动段电流），故并入其余不变串后总理论
电流 f(x) 也单调不减，只是存在平台（该组被旁路的区段、以及该串已达
开路电压的测点）。因此“|f(x) − I_测| ≤ 容差”的合格网格必为**连续
区间**（可能为空、可能覆盖整段平台），逐点区间取交即该组的可行带。

所以不能对“全部点合格”做朴素布尔二分（f 有平台，谓词可能整段恒真、
也可能左端恒假；且要裁决的是**两个端**而非单一零点），而是对**每个
测点分别二分其合格带的最小/最大端**，再取交点。二分全程调用与穷举
完全相同的 ``string_curve`` / ``string_current``（曲线按网格值缓存），
拐点、零容差、达 Voc 零电流支路的判定与原实现逐位一致；若极端浮点
噪声破坏单调性自检，则对该组退回线性扫描，保证结论与穷举永远相同。
"""

import math
from typing import List, Optional, Sequence, Tuple

from .model import Group, MERGE_EPS, string_curve, string_current, total_current

# 理论电流与实测电流比较时的浮点容差（输入量化到 0.01，远小于物理量纲）
_EPS = 1e-9
# 单组 Isc 退化假设的量化步长：至多两位小数
GRID = 0.01


class DiagnoseError(ValueError):
    """诊断入参非法（调用方映射为 HTTP 400）。"""


def _finite_nonneg(raw, field: str, *, allow_zero: bool = True) -> float:
    """解析有限非负数。

    接受 int/float（拒绝 bool）或可被 float() 解析为有限数的字符串
    （允许 "1.5"、"1e3" 等）；NaN / Infinity / None / 非数值文本一律拒绝。
    """
    if isinstance(raw, bool):
        raise DiagnoseError(f"{field} 必须是有限数值")
    if isinstance(raw, int):
        value = float(raw)
    elif isinstance(raw, float):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            raise DiagnoseError(f"{field} 必须是有限数值（收到 {raw!r}）")
    else:
        raise DiagnoseError(f"{field} 必须是有限数值")
    if not math.isfinite(value):
        raise DiagnoseError(f"{field} 必须是有限数值")
    if value < 0.0 or (not allow_zero and value <= 0.0):
        raise DiagnoseError(f"{field} 必须为非负数")
    return value


def validate_diagnose_request(payload: dict, baseline_voc: float) -> List[tuple]:
    """校验诊断请求，返回 [(电压 V, 电流 I), ...]（电压两两互异）。

    规则：
    - points 至少 3 个；每个点含 v、i，均为有限非负数；
    - 电压可为零，但不得超过该记录基线开路电压 baseline_voc；
    - 电压必须两两互异；
    - tolerance 为有限非负数（可为零）。
    """
    if not isinstance(payload, dict):
        raise DiagnoseError("请求体必须是 JSON 对象")

    if "tolerance" not in payload:
        raise DiagnoseError("缺少电流容差 tolerance")
    tol = _finite_nonneg(payload.get("tolerance"), "电流容差")

    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise DiagnoseError("points 必须为数组")
    if len(raw_points) < 3:
        raise DiagnoseError("至少提交 3 个实测 (V,I) 测点")

    points: List[tuple] = []
    seen_v: set = set()
    for idx, p in enumerate(raw_points):
        if not isinstance(p, dict):
            raise DiagnoseError(f"第 {idx + 1} 个测点格式错误")
        v = _finite_nonneg(p.get("v"), f"第 {idx + 1} 个测点的电压")
        i = _finite_nonneg(p.get("i"), f"第 {idx + 1} 个测点的电流")
        if v > baseline_voc + 1e-7:
            raise DiagnoseError(
                f"第 {idx + 1} 个测点电压 {_num(v)} V 超过该记录基线开路电压 "
                f"{_num(baseline_voc)} V"
            )
        # 电压按原始数值去重（浮点输入，精确比较；字符串经 float 归一化）
        v_key = round(v, 12)
        if v_key in seen_v:
            raise DiagnoseError(f"第 {idx + 1} 个测点电压与前点重复，电压必须互异")
        seen_v.add(v_key)
        points.append((v, i))

    payload["tolerance"] = tol  # 回写归一化值，供主流程使用
    return points


def _num(x: float) -> str:
    """去掉无意义尾零的紧凑数值文本。"""
    return f"{x:.10f}".rstrip("0").rstrip(".")


def _grid_values(isc0: float) -> List[float]:
    """小于 isc0 的全部正两位小数候选（升序）：0.01, 0.02, ..., < isc0。"""
    n = int(math.floor(isc0 / GRID + 1e-9))
    if n * GRID >= isc0 - 1e-9:
        n -= 1  # 等档（原 Isc 本身）不属于退化假设
    return [k * GRID for k in range(1, n + 1)]


def _within(theory: float, measured: float, tol: float) -> bool:
    return abs(theory - measured) <= tol + _EPS


def _lower_bound_suffix(ok, n: int) -> int:
    """对**后缀型**单调谓词 ok(k)（前假后真）求首个为真的网格下标。

    返回值取 1..n+1：n+1 表示全部为假。
    """
    if ok(1):
        return 1
    if not ok(n):
        return n + 1
    lo, hi = 1, n
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _upper_bound_prefix(ok, n: int) -> int:
    """对**前缀型**单调谓词 ok(k)（前真后假）求末个为真的网格下标。

    返回值取 0..n：0 表示全部为假。
    """
    if ok(n):
        return n
    if not ok(1):
        return 0
    lo, hi = 1, n
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _brute_force_group_band(group: Group, string_groups, pos: int,
                            other_curves, points: Sequence[tuple],
                            tol: float) -> Tuple[Optional[int], Optional[int]]:
    """兜底：逐 0.01 档重建变化串并逐点比较（与历史实现完全相同的枚举）。

    仅当浮点噪声使理论电流的单调性自检失败（正常输入下不会发生）时启用，
    保证任何情况下结论都与穷举逐档枚举逐位一致。
    """
    lo = hi = None
    for k, isc_new in enumerate(_grid_values(group.isc), start=1):
        members = [
            Group(group.id, group.voc, isc_new) if j == pos else mg
            for j, mg in enumerate(string_groups)
        ]
        curve_new = string_curve(members)
        # 求和次序与历史实现一致：其余不变串在前、变化串在末
        if all(_within(total_current(list(other_curves) + [curve_new], v),
                       i_meas, tol)
               for v, i_meas in points):
            if lo is None:
                lo = k
            hi = k
    return lo, hi


def _point_band(theory, accept, n: int, i_meas: float, tol: float
                ) -> Tuple[Optional[int], Optional[int]]:
    """裁决单个测点在 1..n 网格上的合格带 (lo, hi)。

    理论电流关于退化 Isc 单调不减（允许平台），故合格条件
    ``theory ∈ [I_测−容差, I_测+容差]`` 为连续区间：
    分别对两个阈值谓词（后缀型 ≥ 下限、前缀型 ≤ 上限）二分求端，
    再以**逐点真实容差判定** accept 对两端做贴靠修正（吸收拐点选段的
    ulp 级噪声）。返回 (None, None) 表示该测点无任何合格档；
    若谓词形态与单调结构矛盾（噪声造成断裂），返回 (0, 0) 由调用方
    退回整组线性枚举。
    """
    lo_lim = i_meas - tol - _EPS
    hi_lim = i_meas + tol + _EPS
    lo = _lower_bound_suffix(lambda k: theory(k) >= lo_lim, n)
    hi = _upper_bound_prefix(lambda k: theory(k) <= hi_lim, n)

    if lo > hi:
        # 数学上即“空档”（理论值整体越过容差窗，或相邻两档跨过整窗）；
        # 若边界点竟被真实判定接受，说明阈值谓词与容差判定不一致 -> 兜底。
        if (lo <= n and accept(lo)) or (hi >= 1 and accept(hi)):
            return 0, 0
        return None, None

    # 低端向上贴靠到首个真实合格档；再向左吞掉连续合格档（平台/噪声）
    while lo <= hi and not accept(lo):
        lo += 1
    if lo > hi:
        # 候选窗内竟无真实合格档：检查窗外紧邻点，矛盾则整组兜底
        if (hi >= 1 and accept(hi)) or (lo <= n and accept(lo)):
            return 0, 0
        return None, None
    while lo > 1 and accept(lo - 1):
        lo -= 1

    # 高端向下贴靠到末个真实合格档；再向右吞掉连续合格档
    while hi >= lo and not accept(hi):
        hi -= 1
    if hi < lo:
        return 0, 0
    while hi < n and accept(hi + 1):
        hi += 1

    # 单调性自检：端外的阈值谓词必须与端状态一致
    if lo > 1 and theory(lo - 1) >= lo_lim + 1e-7:
        return 0, 0
    if hi < n and theory(hi + 1) <= hi_lim - 1e-7:
        return 0, 0
    return lo, hi


def _feasible_band(group: Group, string_groups, pos: int, other_curves,
                   points: Sequence[tuple], tol: float
                   ) -> Tuple[Optional[int], Optional[int]]:
    """求某组 Isc 退化可解释**全部**测点的 0.01 网格下标闭区间。

    逐点二分合格带两端后取交（每点合格带为连续区间，交仍为区间）。
    返回 (None, None) 表示无可行档；单调性自检失败时对该组退回枚举。
    """
    grid = _grid_values(group.isc)
    n = len(grid)
    if n == 0:
        return None, None

    # 同一网格档的变化串曲线只构建一次，供该组全部测点共享
    cache: dict = {}

    def curve_at(k: int):
        c = cache.get(k)
        if c is None:
            members = [
                Group(group.id, group.voc, grid[k - 1]) if j == pos else mg
                for j, mg in enumerate(string_groups)
            ]
            c = string_curve(members)
            cache[k] = c
        return c

    # 其余不变串的支路电流之和与退化档无关，每测点只算一次
    other_sum = [sum(string_current(c, v) for c in other_curves)
                 for v, _ in points]

    lo_all, hi_all = 1, n
    for p_idx, (v, i_meas) in enumerate(points):
        def theory(k: int, base=other_sum[p_idx], vv=v):
            return base + string_current(curve_at(k), vv)

        def accept(k: int, i_meas=i_meas):
            return _within(theory(k), i_meas, tol)

        plo, phi = _point_band(theory, accept, n, i_meas, tol)
        if plo == 0:  # 单调性自检失败：该组退回逐档穷举
            return _brute_force_group_band(
                group, string_groups, pos, other_curves, points, tol)
        if plo is None:
            return None, None
        lo_all = max(lo_all, plo)
        hi_all = min(hi_all, phi)
        if lo_all > hi_all:
            return None, None

    # 终检：交带两端必须确实逐点合格、紧邻外档必须至少有一点否决
    for k_bad in (lo_all - 1, hi_all + 1):
        if 1 <= k_bad <= n and all(
                _within(other_sum[j]
                        + string_current(curve_at(k_bad), v), i_meas, tol)
                for j, (v, i_meas) in enumerate(points)):
            return _brute_force_group_band(
                group, string_groups, pos, other_curves, points, tol)
    return lo_all, hi_all


def diagnose(groups: Sequence[Group], baseline: Sequence[Sequence[str]],
             points: Sequence[tuple], tol: float) -> dict:
    """执行现场偏差定位。

    返回结构见模块开头说明：含健康假设逐点偏差、单组退化可行候选的
    Isc 网格区间，以及最终中文结论。
    """
    by_id = {g.id: g for g in groups}
    # 基线串曲线：组内顺序无关，按编号排序以与试算主流程保持一致
    base_strings = [
        [by_id[gid] for gid in sorted(s, key=lambda g: by_id[g].id)]
        for s in baseline
    ]
    base_curves = [string_curve(s) for s in base_strings]
    baseline_voc = max(c.voc for c in base_curves)

    # 组 -> 所在 (串索引, 串内位置)，退化时只重建这一条串
    string_of_group = {}
    for s_idx, string in enumerate(base_strings):
        for pos, g in enumerate(string):
            string_of_group[g.id] = (s_idx, pos)

    # ---- 健康假设：全部测点理论电流与逐点偏差 ----
    healthy_points = []
    healthy_ok = True
    for v, i_meas in points:
        i_th = total_current(base_curves, v)
        dev = i_meas - i_th
        fit = _within(i_th, i_meas, tol)
        healthy_ok = healthy_ok and fit
        healthy_points.append({
            "v": v, "i_measured": i_meas,
            "i_theory_healthy": _r(i_th), "deviation": _r(dev),
            "within_tolerance": fit,
        })

    # ---- 单组 Isc 退化假设：逐组在 0.01 网格上裁决可行带 ----
    group_results = []
    for g in groups:
        s_idx, pos = string_of_group[g.id]
        others = [c for j, c in enumerate(base_curves) if j != s_idx]
        # 该组所在串“端电压严格小于其 Voc、仍贡献非零电流”的测点数量
        # （仅用于结果展示；V 恰等于 Voc 时该串恒为零，对组退化不敏感）。
        # 注意：不在此处跳过这些测点——开路支路对总电流贡献 0，
        # 该组退化不会改变总电流，若实测偏离健康值则候选自然全部落空，
        # 不能因“无约束”而真空判为可行。
        sensitive_points = sum(
            1 for v, _ in points if v < base_curves[s_idx].voc - MERGE_EPS)
        lo, hi = _feasible_band(
            g, base_strings[s_idx], pos, others, points, tol)
        feasible = lo is not None
        entry = {
            "group": g.id,
            "isc_original": _r(g.isc),
            "sensitive_points": sensitive_points,
            "feasible": feasible,
        }
        if feasible:
            isc_lo = lo * GRID
            isc_hi = hi * GRID
            entry["isc_min"] = _r2(isc_lo)
            entry["isc_max"] = _r2(isc_hi)
            entry["isc_candidates"] = hi - lo + 1
        group_results.append(entry)

    explainable = [e for e in group_results if e["feasible"]]

    # ---- 结论判定 ----
    if healthy_ok:
        verdict = "unconfirmed"
        message = ("健康状态即可解释全部测点偏差，无法确认异常"
                   + ("；同时存在可解释的单组退化假设，但不足以判定为故障。"
                      if explainable else "。"))
    elif len(explainable) == 0:
        verdict = "out_of_model"
        message = ("全部健康与单组 Isc 退化假设均无法解释测点偏差："
                   "模型外偏差（可能存在多组退化、Voc 漂移或接线改变）。")
    elif len(explainable) == 1:
        e = explainable[0]
        verdict = "single_group"
        if e["isc_min"] == e["isc_max"]:
            rng = f"Isc = {e['isc_min']}"
        else:
            rng = f"{e['isc_min']} ≤ Isc ≤ {e['isc_max']}"
        message = (f"定位到单组退化：组 {e['group']} 的短路电流可解释全部"
                   f"测点偏差，可行 Isc 范围 {rng} A（共 "
                   f"{e['isc_candidates']} 个 0.01 网格取值，"
                   f"原 Isc {e['isc_original']} A）。")
    else:
        verdict = "ambiguous"
        names = "、".join(
            f"组 {e['group']}（{e['isc_min']}～{e['isc_max']} A）"
            if e["isc_min"] != e["isc_max"]
            else f"组 {e['group']}（{e['isc_min']} A）"
            for e in explainable
        )
        message = f"存在多个可解释的单组退化假设，组间歧义：{names}。"

    return {
        "baseline_voc": _r(baseline_voc),
        "tolerance": tol,
        "points": healthy_points,
        "healthy_explainable": healthy_ok,
        "verdict": verdict,
        "message": message,
        "groups": group_results,
        "explainable_groups": [e["group"] for e in explainable],
    }


def _r(x: float) -> float:
    """理论电流/偏差保留 6 位小数。"""
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def _r2(x: float) -> float:
    """退化 Isc 网格值保留 2 位小数。"""
    return round(float(x) + 0.0, 2)
