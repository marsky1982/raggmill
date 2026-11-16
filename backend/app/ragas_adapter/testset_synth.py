"""Step 2 合成器容错与分阶段编排：人格名称归一化、多跳概念对齐、_PhasedGeneration。

从 testset.py 拆出（纯搬移，行为不变）；对外仍从 `app.ragas_adapter.testset` 导入。
"""
from __future__ import annotations

import asyncio
import re
import threading


def _term_key(term: str) -> str:
    """概念比较用的规范形：全半角统一、小写、「与/和/及/&/and」视为同一连接词、去掉空白与标点。"""
    import re
    import unicodedata

    text = unicodedata.normalize("NFKC", str(term or "")).lower()
    text = re.sub(r"\s*(与|和|及|跟|以及|&|\band\b)\s*", "&", text)
    return re.sub(r"[\s\-_·,，;；:：'\"“”‘’()\[\]【】<>《》/\\.。!?！？]+", "", text)


def _canonical_term(term: str, vocab: list[str]) -> str | None:
    """把一个概念对齐到 vocab 里的某个真实主题 / 实体；对不上返回 None（调用方保留原词）。

    顺序：完全一致 → 规范形一致 → 互相包含（取重叠最长的那个，且规范形至少 2 个字）→ 相似度 ≥ 0.8。
    """
    import difflib

    if term in vocab:
        return term
    key = _term_key(term)
    if not key:
        return None
    keyed = [(v, _term_key(v)) for v in vocab]
    for v, vk in keyed:
        if vk == key:
            return v
    contained = [(v, vk) for v, vk in keyed if len(vk) >= 2 and len(key) >= 2 and (vk in key or key in vk)]
    if contained:
        return max(contained, key=lambda item: min(len(item[1]), len(key)))[0]
    best, ratio = None, 0.0
    for v, vk in keyed:
        r = difflib.SequenceMatcher(None, key, vk).ratio()
        if r > ratio:
            best, ratio = v, r
    return best if ratio >= 0.8 else None


def _align_multihop_concepts(nodes, combinations, persona_item_mapping, property_name: str):
    """把 LLM 产出的概念组合与人格映射里的概念，对齐到簇内节点真实存在的 themes / entities。

    背景（2026-11-06 rc3 实测）：ragas 多跳合成器的 prepare_combinations 用**逐字相等**筛节点和人格
    （`concept.lower() in node_themes`），而概念组合、人格匹配都是 LLM 自由输出——改写一个字
    （「与」写成「和」、多一个空格）整条组合就被滤掉。小图往往只有 1 个簇，没有别的簇兜底，于是该题型 0 场景。
    对不上的概念保留原词，不强行替换；返回 (组合, 映射, 对齐次数)。
    """
    vocab: list[str] = []
    for node in nodes:
        for item in node.properties.get(property_name) or []:
            if isinstance(item, str) and item not in vocab:
                vocab.append(item)
    if not vocab:
        return combinations, persona_item_mapping, 0
    aligned = 0

    def fix(term):
        nonlocal aligned
        if not isinstance(term, str):
            return term
        canon = _canonical_term(term, vocab)
        if canon is not None and canon != term:
            aligned += 1
            return canon
        return term

    new_combinations = [[fix(c) for c in combo] for combo in combinations]
    new_mapping = {name: [fix(c) for c in (concepts or [])] for name, concepts in (persona_item_mapping or {}).items()}
    return new_combinations, new_mapping, aligned


def _build_persona_safe_synthesizer(key: str, llm, llm_ctx: str | None,
                                    abstract_relation_property: str):
    """构造对 persona 名称更宽容的合成器子类（ragas 严格匹配 → 归一化回退）。

    子类只覆写 prepare_combinations：在调用 ragas 原实现前，把 LLM 返回的映射键
    改写成人格真实名称；这样即使模型写成 "Chen Wei (角色描述)" 也不会 KeyError 整类丢题。
    """
    from ragas.testset.synthesizers.multi_hop.abstract import MultiHopAbstractQuerySynthesizer
    from ragas.testset.synthesizers.multi_hop.specific import MultiHopSpecificQuerySynthesizer
    from ragas.testset.synthesizers.single_hop.specific import SingleHopSpecificQuerySynthesizer

    class _SafeSingleHop(SingleHopSpecificQuerySynthesizer):
        def prepare_combinations(self, node, terms, personas, persona_concepts):
            mapping, remapped = _normalize_persona_mapping(persona_concepts, personas)
            self._persona_remaps = remapped
            return super().prepare_combinations(node, terms, personas, mapping)

    class _SafeMultiHopAbstract(MultiHopAbstractQuerySynthesizer):
        def prepare_combinations(self, nodes, combinations, personas, persona_item_mapping, property_name):
            mapping, remapped = _normalize_persona_mapping(persona_item_mapping, personas)
            self._persona_remaps = getattr(self, "_persona_remaps", 0) + remapped
            combinations, mapping, aligned = _align_multihop_concepts(nodes, combinations, mapping, property_name)
            self._concept_aligns = getattr(self, "_concept_aligns", 0) + aligned
            return super().prepare_combinations(nodes, combinations, personas, mapping, property_name)

    class _SafeMultiHopSpecific(MultiHopSpecificQuerySynthesizer):
        def prepare_combinations(self, nodes, combinations, personas, persona_item_mapping, property_name):
            mapping, remapped = _normalize_persona_mapping(persona_item_mapping, personas)
            self._persona_remaps = getattr(self, "_persona_remaps", 0) + remapped
            combinations, mapping, aligned = _align_multihop_concepts(nodes, combinations, mapping, property_name)
            self._concept_aligns = getattr(self, "_concept_aligns", 0) + aligned
            return super().prepare_combinations(nodes, combinations, personas, mapping, property_name)

    if key == "single_hop_specific":
        return _SafeSingleHop(llm=llm, llm_context=llm_ctx)
    if key == "multi_hop_abstract":
        return _SafeMultiHopAbstract(llm=llm, llm_context=llm_ctx,
                                     relation_property=abstract_relation_property)
    return _SafeMultiHopSpecific(llm=llm, llm_context=llm_ctx)


_executor_cls_cache: dict[str, type] = {}


def _new_executor(**kwargs):
    """构造 ragas Executor；首次调用时才定义「取消安全」子类（ragas 保持惰性 import，demo 降级仍可用）。

    背景（2026-11-06 实测）：ragas 的 Executor 在 ``_process_coroutines`` 里**先把全部 job 包成协程**，
    再按 max_workers 用信号量放行；取消时它只 cancel 排队中的 task 就 break，被包在 task 里、
    还没轮到执行的那个协程既不会被 await 也不会被 close，析构时抛
    ``RuntimeWarning: coroutine 'Executor.wrap_callable_with_index.<locals>.wrapped_callable_async'
    was never awaited``。这只是 stderr 噪声（任务确实已中断、无资源泄漏），但会淹没真警告。

    这里只重写调度这一处：取消时先 cancel 未完成的 task，再把「从未被 await」的协程显式 ``close()``。
    取消语义与 ragas 完全一致：不吞 CancelledError、不延后取消生效、不影响在途 job 的异常上报。

    **两条路径都覆盖**（2026-11-14 起；原先的「两道拒绝兜底」已撤）：``max_workers`` 非批量路径走
    ``_process_coroutines``；``batch_size`` 非空的分批路径走 ``_process_batched_jobs``——保留 ragas
    的分批与嵌套进度条形状，每批复用同一套取消安全调度：批前 ``is_cancelled()`` 就停发后续批次
    （后续批次的协程根本不会创建），批内取消则 cancel 在途 task 并 close 从未 await 的协程。
    异常上报与非批量路径同口径：``raise_exceptions=True`` 立即抛，``False`` 记 ``(idx, nan)``
    并继续跑完剩余批次。
    """
    if "cls" not in _executor_cls_cache:
        from ragas.executor import Executor
        from ragas.utils import batched

        class _CancelSafeExecutor(Executor):
            async def _process_batched_jobs(self, jobs_to_process, progress_manager, max_workers, results):
                """分批调度：形状与 ragas 一致，每批的跑法换成取消安全的 ``_process_coroutines``。"""
                batch_size = self.batch_size or len(jobs_to_process)
                batches = batched(jobs_to_process, batch_size)
                overall_pbar, batch_pbar, n_batches = progress_manager.create_nested_bars(
                    len(jobs_to_process), batch_size
                )

                with overall_pbar, batch_pbar:
                    for i, batch in enumerate(batches, 1):
                        # 与 ragas 一致：批前检查取消，命中就停发后续批次（这些协程还没创建，不会泄漏）
                        if self.is_cancelled():
                            break
                        progress_manager.update_batch_bar(batch_pbar, i, n_batches, len(batch))
                        await self._process_coroutines(batch, batch_pbar, results, max_workers)
                        overall_pbar.update(len(batch))

            async def _process_coroutines(self, jobs, pbar, results, max_workers):
                coroutines = [afunc(*args, **kwargs) for afunc, args, kwargs, _ in jobs]
                started = [False] * len(coroutines)
                semaphore = asyncio.Semaphore(max_workers) if max_workers != -1 else None

                async def _run(index, coro):
                    if semaphore is None:
                        started[index] = True
                        return await coro
                    async with semaphore:
                        # 标记必须在 await 之前置位，且中间无挂起点：这样 finally 里 close 时
                        # 能准确区分「已被 await（可能仍在途）」与「从未 await」
                        started[index] = True
                        return await coro

                tasks = [asyncio.create_task(_run(i, coro)) for i, coro in enumerate(coroutines)]
                iterator = asyncio.as_completed(tasks)
                try:
                    # 取消检查必须在「取下一个 future」之前：as_completed 的 plain iterator 每次
                    # next() 都会新建一个 _wait_for_one 协程，先 next 再 break 会留下未 await 的协程。
                    while not self.is_cancelled():
                        try:
                            future = next(iterator)
                        except StopIteration:
                            break
                        try:
                            result = await future
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # ragas 的包装已把 job 失败转成 (idx, nan)，这里只是兜底
                            if self.raise_exceptions:
                                raise
                            continue
                        results.append(result)
                        pbar.update(1)
                finally:
                    # 与 ragas 一致：取消即停发排队中的 job，不等待在途任务自然结束
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    for i, coro in enumerate(coroutines):
                        if not started[i]:
                            coro.close()

        _executor_cls_cache["cls"] = _CancelSafeExecutor
    return _executor_cls_cache["cls"](**kwargs)


class _PhasedGeneration:
    """在工作线程里分阶段驱动 ragas 出题：人格 → 场景 → 样本（编排顺序与 TestsetGenerator.generate 一致）。

    为什么不直接调 ``TestsetGenerator.generate()``：
      · 它是同步黑盒，外层协程 cancel 只能丢弃 Future，线程里的 LLM 调用会照样全部跑完；
      · ``return_executor=True`` 也只交出「样本」阶段的 Executor，人格 + 场景阶段（几十次调用）
        仍在 generate() 内部同步跑完才返回，取消/熔断都管不到；
      · ``raise_exceptions=True``（默认）单条失败整批丢失；=False 又拿不到"哪条失败、已完成哪些"；
      · 它用 ceil 分配条数（10 条按 .5/.25/.25 会出 11 条），且对分到 0 条的合成器照样提交，
        多跳抽象题 ``find_n_indirect_clusters(0)`` 直接报错拖垮整批。
    所以这里按同样的顺序自己编排：每阶段用 ragas Executor（raise_exceptions=False），
    每个 job 外包一层记录器，完成一条记一条；取消时 ``executor.cancel()`` + 把正在 await 的
    驱动协程整体 cancel（Executor 的 cancel_check 只在"下一个 job 完成时"才检查，
    场景阶段一个 job = 一个合成器的全部场景，等它自然完成可能要十几分钟）。

    人格阶段由 ``testset_persona.generate_personas`` 承担（JobExecutor，可中断）；阶段化任务框架
    （`app.tasks.staged`）把三个阶段分别当独立 Stage 在工作线程里跑，所以这里拆成
    ``run_personas`` / ``run_scenarios`` / ``run_samples`` 三个同步方法，各自内部 ``asyncio.run``。

    线程约束：这三个方法在工作线程里执行，**不得调 manager.log**；给用户看的消息一律加锁写
    _notes，由主协程 drain_notes() 后落日志；进度只调 `ctx.advance()`（线程安全缓冲）。
    """

    def __init__(self, llm, kg, active: list[tuple], num_personas: int, run_config, callbacks: list,
                 ctx=None, *, telemetry=None):
        self.llm = llm
        self.kg = kg
        self.active = active                 # [(synthesizer, n), ...]，只含 n>0 的
        self.num_personas = num_personas
        self.run_config = run_config
        self.callbacks = callbacks
        self.ctx = ctx                        # 阶段上下文；由 StagedJob 在调 run_* 时传入
        self.telemetry = telemetry            # 只为把逐次调用明细转发到日志缓冲（可空）
        self.personas: list = []             # 人格阶段产出，场景 job 在调用时取（支持降级为 1 人格）
        self.scenarios: dict[int, list] = {}  # 合成器下标 -> 场景（场景阶段写，样本阶段读）
        self._persona_downgraded = False
        self._top_grp = None                 # 三个 ragas RunTree 共用的顶层分组

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._notes: list[str] = []
        self._exec = None                    # 当前阶段的 ragas Executor
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task | None = None

        self.phase = "pending"               # pending / personas / scenarios / samples / done
        self.samples: dict[int, object] = {}  # 样本序号 -> ragas eval_sample（只记成功的）
        self.sample_nodes: dict[int, list[str]] = {}  # 样本序号 -> 场景节点 id
        self.sample_synth: dict[int, str] = {}
        self.sample_failures: list[str] = []
        self.scenario_failures: list[str] = []
        self.planned_samples = 0
        self.error: str | None = None

    # ---------- 主协程侧（线程安全） ----------

    def cancel(self) -> None:
        """可从任意线程调用：停止发起新调用，并中断当前阶段在途的协程。"""
        self._stop.set()
        with self._lock:
            if self._exec is not None:
                self._exec.cancel()          # ragas 官方取消开关：不再派发排队中的 job
            if self._loop is not None and self._main is not None:
                try:
                    # 在途 job 一并 cancel：asyncio.run 收尾时会取消并回收所有子任务，HTTP 请求随之中断
                    self._loop.call_soon_threadsafe(self._main.cancel)
                except RuntimeError:         # loop 恰好已关闭
                    pass

    def drain_notes(self) -> list[str]:
        with self._lock:
            notes, self._notes = self._notes, []
            return notes

    def snapshot_results(
        self,
    ) -> tuple[list[tuple[str, object]], int, list[str], list[str], dict[int, list[str]]]:
        """按提交顺序返回 [(synthesizer_name, eval_sample), ...]、失败信息，以及与之**同序**的出处。

        成功样本会被压实成 0..n-1（失败 / 取消留下的空洞被跳过），出处必须在同一把锁内
        按同一顺序压实，否则第 k 条之前只要有一条失败，后面的样本就会挂上别人的 chunk（P2-1）。
        返回的出处字典以**压实后的位置**为键，与 ordered / 落盘样本的 index 一致。
        """
        with self._lock:
            keys = sorted(self.samples)
            ordered = [(self.sample_synth.get(i, ""), self.samples[i]) for i in keys]
            nodes = {pos: list(self.sample_nodes.get(i) or []) for pos, i in enumerate(keys)}
            return (ordered, self.planned_samples, list(self.sample_failures),
                    list(self.scenario_failures), nodes)

    def snapshot_sample_nodes(self) -> dict[int, list[str]]:
        """压实后的样本位置 -> 该场景实际引用的节点 id。

        仅供观察；落盘请用 snapshot_results() 同一次返回的出处，避免两次快照之间又完成新样本。
        """
        return self.snapshot_results()[4]

    # ---------- 工作线程侧 ----------

    def _drain_telemetry(self) -> None:
        """把遥测缓冲写进 ctx（线程安全，由主协程落日志）。取消后也不丢已发生的调用明细。"""
        if self.telemetry is None or self.ctx is None:
            return
        for line in self.telemetry.drain():
            self.ctx.log(line)

    def _run_executor(self, ex) -> None:
        """在本线程新建事件循环跑完一个 Executor；被 cancel() 打断时静默返回。"""
        async def _telemetry_loop():
            while True:
                await asyncio.sleep(1.0)
                self._drain_telemetry()

        async def _main():
            with self._lock:
                if self._stop.is_set():
                    return
                self._exec = ex
                self._loop = asyncio.get_running_loop()
                self._main = asyncio.current_task()
            drainer = asyncio.create_task(_telemetry_loop())
            try:
                await ex.aresults()
            finally:
                drainer.cancel()
                try:
                    await drainer
                except asyncio.CancelledError:
                    pass
                self._drain_telemetry()
                with self._lock:
                    self._exec = self._loop = self._main = None

        try:
            asyncio.run(_main())
        except asyncio.CancelledError:
            pass

    def _current_personas(self) -> list:
        with self._lock:
            # 一旦某个题型出现人格名称无法匹配，后续题型统一降级为 1 个人格，避免再浪费 token
            return list(self.personas[:1] if self._persona_downgraded else self.personas)

    def _scenario_job(self, idx: int, synth, sink: dict):
        async def _call(*args, **kwargs):
            call_kwargs = dict(kwargs)
            call_kwargs["persona_list"] = self._current_personas()
            try:
                res = await synth.generate_scenarios(*args, **call_kwargs)
            except asyncio.CancelledError:
                raise
            except KeyError as e:
                # 兜底：即使映射键连归一化都匹配不上，也降级为 1 人格重试，绝不整类静默跳过
                personas = call_kwargs.get("persona_list") or []
                if "persona" not in str(e).lower() or len(personas) <= 1:
                    msg = f"{type(e).__name__}: {str(e)[:300]}"
                    with self._lock:
                        self.scenario_failures.append(f"{synth.name}: {msg}")
                        self._notes.append(f"✗ 题型 {synth.name} 的场景生成失败，该题型本次跳过（其他题型照常）：{msg}")
                    raise
                with self._lock:
                    self._persona_downgraded = True
                    self._notes.append(f"⚠ 题型 {synth.name} 人格名称无法匹配（{str(e)[:160]}），"
                                       f"已降级为 1 个人格重试，不会整类跳过")
                retry_kwargs = dict(call_kwargs, persona_list=list(personas)[:1])
                try:
                    res = await synth.generate_scenarios(*args, **retry_kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as e2:  # noqa: BLE001
                    msg = f"{type(e2).__name__}: {str(e2)[:300]}"
                    with self._lock:
                        self.scenario_failures.append(f"{synth.name}: {msg}")
                        self._notes.append(f"✗ 题型 {synth.name} 降级为 1 人格后仍失败，该题型本次跳过：{msg}")
                    raise
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:300]}"
                with self._lock:
                    self.scenario_failures.append(f"{synth.name}: {msg}")
                    self._notes.append(f"✗ 题型 {synth.name} 的场景生成失败，该题型本次跳过（其他题型照常）：{msg}")
                raise
            if getattr(synth, "_persona_remaps", 0):
                with self._lock:
                    self._notes.append(f"⚠ 题型 {synth.name} 有 {synth._persona_remaps} 处人格名称与 LLM 输出不一致，"
                                       f"已按名称归一化回退匹配（未丢题）")
            if getattr(synth, "_concept_aligns", 0):
                with self._lock:
                    self._notes.append(f"· 题型 {synth.name} 有 {synth._concept_aligns} 个概念与节点主题 / 实体写法不一致，"
                                       f"已对齐到原词后再匹配（避免整簇丢题）")
            # ragas 的 _generate_scenarios 按 ceil(n/节点数或簇数) 逐节点/逐簇累加，直到 ≥n 才停，
            # 最后一批会超出 n。截到 n，保证每类实际出题数 = calculate_split_values 的分配
            n = kwargs.get("n", len(res))
            with self._lock:
                sink[idx] = list(res)[:n]
                if len(res) < n:
                    self._notes.append(f"⚠ 题型 {synth.name} 只凑出 {len(res)}/{n} 个场景"
                                       f"（图谱素材不足），该题型实际少出 {n - len(res)} 条")
            return res
        return _call

    def _sample_job(self, idx: int, synth):
        async def _call(*args, **kwargs):
            #  P2：出处在生成时取自场景节点（scenario.nodes），不做文本反查。
            # 场景节点是 ragas 侧「这条题实际引用了哪些 chunk」的唯一权威来源。
            scenario = kwargs.get("scenario") or (args[0] if args else None)
            node_ids = [str(getattr(node, "id", "")) for node in (getattr(scenario, "nodes", None) or [])]
            node_ids = [value for value in node_ids if value]
            with self._lock:
                self.sample_nodes[idx] = node_ids
            try:
                res = await synth.generate_sample(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:300]}"
                with self._lock:
                    self.sample_nodes.pop(idx, None)
                    self.sample_failures.append(f"#{idx + 1} {synth.name}: {msg}")
                    self._notes.append(f"✗ 第 {idx + 1} 条（{synth.name}）生成失败，已跳过，不影响其他条目：{msg}")
                raise
            with self._lock:
                self.samples[idx] = res
            return res
        return _call

    def _counted(self, job, sink: dict | None = None, key: int | None = None):
        """给提交给 ragas Executor 的 job 包一层计数协程。

        完成后按**实际产出条数**调 `ctx.advance()`（场景阶段 = 该合成器产出的场景数，样本阶段 = 1），
        不再照抄 ragas 两个 tqdm 的 total；顺带在超预算时立即停发排队 job 并中断在途调用。
        """
        async def _call(*args, **kwargs):
            res = await job(*args, **kwargs)
            if sink is not None and key is not None:
                self.ctx.advance(len(sink.get(key) or []) or 1)
            else:
                self.ctx.advance(1)
            if self.ctx.budget_exceeded:
                self.cancel()
            return res
        return _call

    # ---------- 工作线程侧：三个阶段各是一个 Stage ----------

    def run_personas(self, ctx=None) -> None:
        """① 人格阶段：聚类 + JobExecutor 调 LLM（可中断，见 testset_persona）。"""
        from .testset_persona import generate_personas

        self.ctx = ctx or self.ctx
        self.phase = "personas"
        if self._stop.is_set():
            return
        personas = generate_personas(self.kg, self.llm, num_personas=self.num_personas,
                                     callbacks=self.callbacks, ctx=self.ctx,
                                     max_workers=getattr(self.run_config, "max_workers", None))
        with self._lock:
            self.personas = list(personas[: self.num_personas])

    def run_scenarios(self, ctx=None) -> None:
        """② 场景阶段：每个合成器一个 job；完成后按产出场景数计数。"""
        from ragas.callbacks import new_group

        self.ctx = ctx or self.ctx
        self.phase = "scenarios"
        if self._stop.is_set():
            return
        total = sum(n for _, n in self.active)
        _, self._top_grp = new_group(name="ragas testset generation",
                                     inputs={"testset_size": total}, callbacks=self.callbacks)
        sc_rm, sc_grp = new_group(name="Scenario Generation",
                                  inputs={"splits": [n for _, n in self.active]},
                                  callbacks=self._top_grp)
        self.ctx.set_stage("scenarios", total)
        ex = _new_executor(desc="Generating Scenarios", raise_exceptions=False,
                           run_config=self.run_config, keep_progress_bar=False)
        # 交给框架持有：取消时 runner.cancel() 既停发排队 job，也中断在途协程
        self.ctx.attach_executor(self)
        with self._lock:
            self.scenarios = {}
        for i, (synth, n) in enumerate(self.active):
            ex.submit(self._counted(self._scenario_job(i, synth, self.scenarios), self.scenarios, i),
                      n=n, knowledge_graph=self.kg, persona_list=self._current_personas(),
                      callbacks=sc_grp)
        self._run_executor(ex)
        sc_rm.on_chain_end(outputs={"scenario_counts": {str(i): len(v) for i, v in self.scenarios.items()}})

    def run_samples(self, ctx=None) -> None:
        """③ 样本阶段：每个场景一个 job，完成一条计一条。"""
        from ragas.callbacks import new_group

        self.ctx = ctx or self.ctx
        self.phase = "samples"
        if self._stop.is_set():
            return
        sm_rm, sm_grp = new_group(name="Sample Generation",
                                  inputs={"scenario_count": sum(len(v) for v in self.scenarios.values())},
                                  callbacks=self._top_grp or self.callbacks)
        planned = 0
        for i, _ in enumerate(self.active):
            planned += len(self.scenarios.get(i) or [])
        self.ctx.set_stage("samples", planned)
        with self._lock:
            self.planned_samples = planned
        if planned == 0:
            self.error = "所有题型的场景生成都失败了，没有可出的题"
            return
        ex = _new_executor(desc="Generating Samples", raise_exceptions=False,
                           run_config=self.run_config, keep_progress_bar=False)
        self.ctx.attach_executor(self)
        idx = 0
        for i, (synth, _) in enumerate(self.active):
            for sc in self.scenarios.get(i) or []:
                ex.submit(self._counted(self._sample_job(idx, synth)), scenario=sc, callbacks=sm_grp)
                with self._lock:
                    self.sample_synth[idx] = synth.name
                idx += 1
        self._run_executor(ex)
        sm_rm.on_chain_end(outputs={"sample_count": len(self.samples)})
        self.phase = "done"


def _persona_name_key(name: str) -> str:
    """persona 名称归一化：去括号角色描述、合并空白、忽略大小写与首尾标点。"""
    text = str(name or "").strip().lower()
    # 模型常把角色描述塞进括号（"Chen Wei (State Grid ...)"），先整体去掉括号内容
    text = re.sub(r"[（(][^）)]*[）)]", " ", text)
    # 也兼容 "Chen Wei - State Grid ..." / "Chen Wei — ..." 这类附加描述
    text = re.split(r"\s+(?:-|—|–)\s+", text, maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n.,;:，。；：、·-—_/\\\"'“”‘’")


def _match_persona(name: str, personas: list):
    """按 exact → 归一化相等 → 前缀包含的顺序匹配人格；匹配不到返回 None。"""
    for persona in personas:
        if persona.name == name:
            return persona
    target = _persona_name_key(name)
    if not target:
        return None
    for persona in personas:
        if _persona_name_key(persona.name) == target:
            return persona
    for persona in personas:
        candidate = _persona_name_key(persona.name)
        if candidate and (target.startswith(candidate) or candidate.startswith(target)):
            return persona
    return None


def _normalize_persona_mapping(mapping: dict | None, personas: list) -> tuple[dict, int]:
    """把 LLM 返回的 persona→concepts 映射键改写成 persona 真实名称。

    ragas 的 ThemesPersonasMatchingPrompt 让模型以人格名为 key 输出映射，模型偶尔会把
    角色描述一起写进 key；ragas 的 PersonaList 是严格相等匹配，一处不一致就 KeyError，
    导致整个题型的场景全部丢失。这里做归一化回退，实在匹配不上的归并到第一个人格，
    保证不会因为命名差异整类跳过；返回 (规范化映射, 需要回退的 key 数)。
    """
    if not mapping or not personas:
        return {}, 0
    normalized: dict[str, list] = {}
    remapped = 0
    for key, concepts in mapping.items():
        persona = _match_persona(key, personas)
        if persona is None:
            persona = personas[0]
            remapped += 1
        elif persona.name != key:
            remapped += 1
        normalized.setdefault(persona.name, []).extend(list(concepts or []))
    return normalized, remapped
