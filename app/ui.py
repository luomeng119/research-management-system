# -*- coding: utf-8 -*-
"""
AI进化沙盘 - Tkinter UI
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import json
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.simulator import generate_stage3_report, generate_stage4_report


def launch_ui():
    root = tk.Tk()
    root.title("AI 进化沙盘 - 硅基智能推演系统")
    root.geometry("1100x750")
    root.minsize(900, 600)
    app = AIEvolutionSandbox(root)
    root.mainloop()


class AIEvolutionSandbox:
    def __init__(self, root):
        self.root = root
        self.report = None
        self._setup_ui()

    def _setup_ui(self):
        # === 顶部标题栏 ===
        title_frame = tk.Frame(self.root, bg='#1a1a2e', height=50)
        title_frame.pack(fill=tk.X)
        title_frame.pack_propagate(False)

        tk.Label(
            title_frame,
            text="AI 进化沙盘 · 硅基智能推演系统",
            font=("Microsoft YaHei", 15, "bold"),
            fg="white",
            bg='#1a1a2e'
        ).pack(pady=12)

        # === 主区域 ===
        main = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=4)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        # --- 左面板 ---
        left = tk.Frame(main, width=300, bg='#f5f5f5')
        main.add(left, width=300)

        # Stage 2 现状
        lf1 = tk.LabelFrame(left, text="Stage 2 现状（已知条件）", font=("Microsoft YaHei", 9, "bold"), padx=8, pady=5)
        lf1.pack(fill=tk.X, pady=(0, 8))

        stage2_items = [
            "· 硅基智能拥有全量人类知识",
            "· 感知：多模态输入、数字世界感知",
            "· 执行：调用工具、操作数字系统",
            "· 记忆：跨会话积累",
            "· 规划：分解复杂任务",
            "· 协作：多Agent协同",
            "· 学习：从反馈中改进",
            "· 探索：主动发现数字世界新规律",
        ]
        for item in stage2_items:
            tk.Label(lf1, text=item, font=("Microsoft YaHei", 8), anchor='w', justify='left').pack(fill=tk.X)

        # Stage 3 临界条件
        lf2 = tk.LabelFrame(left, text="Stage 3 临界条件（已知）", font=("Microsoft YaHei", 9, "bold"), padx=8, pady=5)
        lf2.pack(fill=tk.X, pady=(0, 8))

        thresholds = [
            "武器装备：有人 → 无人参与",
            "指挥模式：有人指挥 → 无人系统指挥",
            "保障模式：体系化无人自主保障",
            "作战模式：成体系无人自主作战",
            "力量形态：无人平台为主",
        ]
        for t in thresholds:
            tk.Label(lf2, text=t, font=("Microsoft YaHei", 8), anchor='w', justify='left').pack(fill=tk.X)

        # 推演选项
        lf3 = tk.LabelFrame(left, text="推演选项", font=("Microsoft YaHei", 9, "bold"), padx=8, pady=5)
        lf3.pack(fill=tk.X, pady=(0, 8))

        self.include_s3 = tk.BooleanVar(value=True)
        self.include_s4 = tk.BooleanVar(value=True)
        tk.Checkbutton(lf3, text="推演 Stage 3（操控物理世界）", variable=self.include_s3, font=("Microsoft YaHei", 9)).pack(anchor='w')
        tk.Checkbutton(lf3, text="推演 Stage 4（0-1 原始创新）", variable=self.include_s4, font=("Microsoft YaHei", 9)).pack(anchor='w')

        # 额外说明
        note = tk.Label(left, text="Stage 4 从 Stage 3 演化而来，不需要额外触发条件。", font=("Microsoft YaHei", 7), fg="gray", wraplength=280, justify='left')
        note.pack(pady=(0, 8))

        # 按钮
        self.btn_run = tk.Button(
            left, text="▶ 开始推演", font=("Microsoft YaHei", 11, "bold"),
            bg="#4CAF50", fg="white", relief=tk.RAISED, height=2,
            command=self._on_run
        )
        self.btn_run.pack(fill=tk.X, pady=(0, 4))

        self.btn_export = tk.Button(
            left, text="📄 导出报告", font=("Microsoft YaHei", 10),
            state='disabled', command=self._on_export
        )
        self.btn_export.pack(fill=tk.X)

        self.lbl_status = tk.Label(left, text="就绪", font=("Microsoft YaHei", 8), fg="gray", anchor='w')
        self.lbl_status.pack(fill=tk.X, pady=(4, 0))

        # --- 右面板：报告展示 ---
        right = tk.Frame(main)
        main.add(right, width=780)

        tk.Label(right, text="推演报告", font=("Microsoft YaHei", 12, "bold")).pack(anchor='w', pady=(0, 5))

        self.report_text = scrolledtext.ScrolledText(
            right, font=("Microsoft YaHei", 10),
            wrap=tk.WORD, bg="#fafafa",
            relief=tk.SOLID, borderwidth=1
        )
        self.report_text.pack(fill=tk.BOTH, expand=True)

        self._show_welcome()

    def _show_welcome(self):
        welcome = (
            "═" * 62 + "\n\n"
            "        AI 进化沙盘 · 硅基智能推演系统\n\n"
            "═" * 62 + "\n\n"
            "  Stage 1  硅基智能拥有全量人类知识（已知）\n\n"
            "  Stage 2  操控数字世界（已知）\n"
            "           感知 / 执行 / 记忆 / 规划 / 协作 / 学习 / 探索发现\n\n"
            "  Stage 3  操控物理世界\n"
            "           → 军事：武器装备 / 指挥 / 保障 / 作战 / 力量形态\n"
            "           → 民用：开放推演\n\n"
            "  Stage 4  0-1 原始创新\n"
            "           → 发现新原理 + 创造物理产品 → 人机协作新生态\n\n"
            "  点击左侧「开始推演」生成完整报告。\n"
        )
        self.report_text.insert(tk.END, welcome)
        self.report_text.config(state='disabled')

    def _on_run(self):
        self.btn_run.config(state='disabled', text="⏳ 推演中...")
        self.lbl_status.config(text="正在推理，请稍候（约3-5分钟）...", fg="orange")
        threading.Thread(target=self._do_run, daemon=True).start()

    def _do_run(self):
        try:
            stage2 = "已知条件：硅基智能拥有全量人类知识，具备感知/执行/记忆/规划/协作/学习/探索发现能力"
            s3, s4 = None, None

            if self.include_s3.get():
                self._update_status("推演 Stage 3（操控物理世界）...")
                s3 = generate_stage3_report(stage2)

            if self.include_s4.get() and s3 and 'error' not in s3:
                self._update_status("推演 Stage 4（0-1 原始创新）...")
                s4 = generate_stage4_report(json.dumps(s3, ensure_ascii=False))

            self.report = {'stage3': s3, 'stage4': s4}
            self._display_report()
            self._update_status(f"推演完成  |  {datetime.datetime.now().strftime('%H:%M:%S')}", fg="green")
            self.btn_export.config(state='normal')
        except Exception as e:
            self._update_status(f"推演失败: {e}", fg="red")
        finally:
            self.btn_run.config(state='normal', text="▶ 开始推演")

    def _update_status(self, msg):
        def _u():
            self.lbl_status.config(text=msg)
        self.root.after(0, _u)

    def _display_report(self):
        def _d():
            self.report_text.config(state='normal')
            self.report_text.delete('1.0', tk.END)
            r = self.report
            if not r:
                return

            lines = []
            W = 62
            lines.append("═" * W)
            lines.append("            AI 进化推演报告")
            lines.append("═" * W)
            lines.append(f"  生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("")

            s3 = r.get('stage3')
            if s3 and 'error' not in s3:
                lines.append("▌ Stage 3 · 操控物理世界")
                lines.append("─" * W)
                theme = s3.get('theme', 'N/A')
                lines.append(f"  主题：{theme}")
                lines.append("")
                for d in s3.get('domains', []):
                    domain = d.get('domain', 'N/A')
                    trigger = d.get('trigger', '')
                    form = d.get('form', '')
                    caps = d.get('capabilities', [])
                    impact = d.get('impact', '')
                    risks = d.get('risks', [])
                    lines.append(f"  【{domain}】")
                    if trigger:
                        lines.append(f"    临界条件：{trigger}")
                    if form:
                        lines.append(f"    AI形态：{form}")
                    if caps:
                        for cap in caps[:3]:
                            lines.append(f"    · {cap}")
                    if impact:
                        lines.append(f"    影响：{impact}")
                    if risks:
                        lines.append(f"    风险：{risks[0]}")
                    lines.append("")
                summary = s3.get('summary', '')
                if summary:
                    lines.append(f"  Stage 3 总结：{summary[:200]}")
                lines.append("")
                transition = s3.get('transition_to_stage4', '')
                if transition:
                    lines.append(f"  → Stage 4 演化路径：{transition[:200]}")
                lines.append("")

            s4 = r.get('stage4')
            if s4 and 'error' not in s4:
                lines.append("▌ Stage 4 · 0-1 原始创新")
                lines.append("─" * W)
                lines.append(f"  主题：{s4.get('theme', 'N/A')}")
                lines.append(f"  核心突破：{s4.get('core_breakthrough', 'N/A')[:200]}")
                lines.append("")
                mech = s4.get('innovation_mechanism', {})
                lines.append("  创新机制：")
                lines.append(f"    发现：{mech.get('discovery', '')[:150]}")
                lines.append(f"    创造：{mech.get('creation', '')[:150]}")
                lines.append(f"    反馈：{mech.get('feedback', '')[:150]}")
                lines.append("")

                hm = s4.get('human_machine_relationship', {})
                lines.append("  人机关系：")
                lines.append(f"    硅基：{hm.get('silicon_role', '')[:150]}")
                lines.append(f"    碳基：{hm.get('carbon_role', '')[:150]}")
                lines.append(f"    协作：{hm.get('collaboration_form', '')[:150]}")
                lines.append("")

                for d in s4.get('domains', []):
                    domain = d.get('domain', 'N/A')
                    caps = d.get('new_capabilities', [])
                    impact = d.get('impact', '')
                    human_role = d.get('human_role', '')
                    risks = d.get('risks', [])
                    lines.append(f"  【{domain}】")
                    if caps:
                        for cap in caps[:2]:
                            lines.append(f"    · {cap[:80]}")
                    if impact:
                        lines.append(f"    影响：{impact[:120]}")
                    if human_role:
                        lines.append(f"    人类角色：{human_role[:100]}")
                    lines.append("")

                final = s4.get('final_ecology', '')
                if final:
                    lines.append(f"  最终生态：{final[:200]}")
                risks = s4.get('risks', [])
                if risks:
                    lines.append(f"  最高风险：{risks[0]}")
                lines.append("")
            elif s4 and 'error' in s4:
                lines.append(f"  Stage 4 生成失败：{s4.get('raw', s4.get('error', ''))[:300]}")

            lines.append("═" * W)
            lines.append("  报告结束")
            lines.append("═" * W)

            self.report_text.insert(tk.END, "\n".join(lines))
            self.report_text.config(state='disabled')

        self.root.after(0, _d)

    def _on_export(self):
        if not self.report:
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="保存推演报告",
            defaultextension=".json",
            filetypes=[("JSON文件", "*.json"), ("文本文件", "*.txt")]
        )
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(self.report, f, ensure_ascii=False, indent=2)
                messagebox.showinfo("导出成功", f"报告已保存到：\n{path}")
            except Exception as e:
                messagebox.showerror("导出失败", str(e))
