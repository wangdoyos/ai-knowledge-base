"""验证 state.KBState 的字段定义与实例创建。

用法：
    python check_state.py
"""

from state import KBState, make_initial_state


def main() -> None:
    annotations = KBState.__annotations__
    print("KBState 字段：")
    for name, type_hint in annotations.items():
        print(f"  {name}: {type_hint}")
    print(f"\n共 {len(annotations)} 个字段")

    # 手工构造一份实例，验证 TypedDict 约定
    state: KBState = {
        "sources": [],
        "analyses": [],
        "articles": [],
        "review_feedback": "",
        "review_passed": False,
        "iteration": 0,
        "cost_tracker": {},
    }
    print(f'实例创建成功，iteration = {state["iteration"]}')

    # 推荐使用 make_initial_state() 构造初始状态（cost_tracker 带完整零值键）
    initial = make_initial_state()
    print(f"make_initial_state() 创建成功，cost_tracker = {initial['cost_tracker']}")


if __name__ == "__main__":
    main()
