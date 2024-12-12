import logging
from .parser import parse_prompt_schedules
from comfy_execution.graph_utils import GraphBuilder

from .prompts import get_function

log = logging.getLogger("comfyui-prompt-control")

from .nodes_hooks import consolidate_schedule, find_nonscheduled_loras
from .utils import lora_name_to_file


class PCLazyLoraLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"multiline": True}),
                "model": ("MODEL", {"rawLink": True}),
                "clip": ("CLIP", {"rawLink": True}),
                "apply_hooks": ("BOOLEAN", {"default": True}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("MODEL", "CLIP", "HOOKS")
    OUTPUT_TOOLTIPS = ("Returns a model and clip with LoRAs scheduled",)
    CATEGORY = "promptcontrol"
    FUNCTION = "apply"

    def apply(self, model, clip, text, apply_hooks, unique_id):
        schedule = parse_prompt_schedules(text)
        consolidated = consolidate_schedule(schedule)
        non_scheduled = find_nonscheduled_loras(consolidated)
        graph = GraphBuilder(f"PCLazyLoraLoader-{unique_id}")
        for lora, info in non_scheduled.items():
            path = lora_name_to_file(lora)
            if path is None:
                log.info("Lazy expansion ignoring nonexistent LoRA %s", lora)
                continue
            loader = graph.node("LoraLoader")
            loader.set_input("model", model)
            loader.set_input("clip", clip)
            loader.set_input("strength_model", info["weight"])
            loader.set_input("strength_clip", info["weight_clip"])
            loader.set_input("lora_name", path)
            model = loader.out(0)
            clip = loader.out(1)

        hook_nodes = {}
        start_pct = 0.0

        def key(lora, info):
            return f"{lora}-{info['weight']}-{info['weight_clip']}"

        for end_pct, loras in consolidated:
            for lora, info in loras.items():
                if non_scheduled.get(lora):
                    continue
                path = lora_name_to_file(lora)
                if path is None:
                    log.info("Lazy expansion ignoring nonexistent LoRA %s", lora)
                    continue
                k = key(lora, info)
                existing_node = hook_nodes.get(key(lora, info))
                prev_keyframe = None
                if not existing_node:
                    hook_node = graph.node("CreateHookLora")
                    hook_node.set_input("lora_name", path)
                    hook_node.set_input("strength_model", info["weight"])
                    hook_node.set_input("strength_clip", info["weight_clip"])
                    prev_hook_kf = None
                    if start_pct > 0:
                        prev_keyframe = graph.node("CreateHookKeyframe")
                        prev_keyframe.set_input("strength_mult", 0.0)
                        prev_keyframe.set_input("start_percent", 0.0)
                        prev_hook_kf = prev_keyframe.out(0)
                else:
                    hook_node, prev_keyframe = existing_node
                    prev_hook_kf = prev_keyframe.out(0)

                if (
                    prev_keyframe
                    and prev_keyframe.get_input("start_pct") == start_pct
                    and prev_keyframe.get_input("strength_mult") == 0.0
                ):
                    next_keyframe = prev_keyframe
                else:
                    next_keyframe = graph.node("CreateHookKeyframe")
                    next_keyframe.set_input("start_percent", start_pct)
                    next_keyframe.set_input("prev_hook_kf", prev_hook_kf)

                next_keyframe.set_input("strength_mult", 1.0)
                prev_hook_kf = next_keyframe.out(0)
                if end_pct < 1.0:
                    next_keyframe = graph.node("CreateHookKeyframe")
                    next_keyframe.set_input("strength_mult", 1.0)
                    next_keyframe.set_input("start_percent", end_pct)
                    next_keyframe.set_input("prev_hook_kf", prev_hook_kf)

                hook_nodes[k] = (hook_node, next_keyframe)
            start_pct = end_pct
        hooks = []
        for hook, kfs in hook_nodes.values():
            n = graph.node("SetHookKeyframes")
            n.set_input("hooks", hook.out(0))
            n.set_input("hook_kf", kfs.out(0))
            hooks.append(n)
        res = None
        if len(hooks) > 0:
            res = hooks[0]
            for h in hooks[:1]:
                n = graph.node("CombineHooks2")
                n.set_input("hooks_A", res.out(0))
                n.set_input("hooks_B", h.out(0))
                res = n
            res = res.out(0)
        if apply_hooks:
            n = graph.node("SetClipHooks")
            n.set_input("clip", clip)
            n.set_input("hooks", res)
            n.set_input("apply_to_conds", True)
            n.set_input("schedule_clip", True)
            clip = n.out(0)

        r = graph.finalize()

        return {"result": (model, clip, res), "expand": r}


class PCLazyTextEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {"clip": ("CLIP", {"rawLink": True}), "text": ("STRING", {"multiline": True})},
            # "optional": {"defaults": ("SCHEDULE_DEFAULTS",)},
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("CONDITIONING",)
    OUTPUT_TOOLTIPS = ("A fully encoded and scheduled conditioning",)
    CATEGORY = "promptcontrol"
    FUNCTION = "apply"

    def apply(self, clip, text, unique_id):
        schedules = parse_prompt_schedules(text)
        graph = GraphBuilder(f"PCEncodeLazy-{unique_id}")

        nodes = []
        start_pct = 0.0
        for end_pct, c in schedules:
            p = c["prompt"]
            p, classnames = get_function(p, "NODE", ["PCTextEncode", "text"])
            classname = "PCTextEncode"
            paramname = "text"
            if classnames:
                classname = classnames[0][0]
                paramname = classnames[0][1]
            node = graph.node(classname)
            timestep = graph.node("ConditioningSetTimestepRange")
            node.set_input("clip", clip)
            node.set_input(paramname, p)
            timestep.set_input("conditioning", node.out(0))
            timestep.set_input("start", start_pct)
            timestep.set_input("end", end_pct)
            nodes.append(timestep)
            start_pct = end_pct
        node = nodes[0]
        for othernode in nodes[1:]:
            combiner = graph.node("ConditioningCombine")
            combiner.set_input("conditioning_1", node.out(0))
            combiner.set_input("conditioning_2", othernode.out(0))
            node = combiner

        return {"result": (node.out(0),), "expand": graph.finalize()}


NODE_CLASS_MAPPINGS = {
    "PCLazyTextEncode": PCLazyTextEncode,
    "PCLazyLoraLoader": PCLazyLoraLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PCLazyTextEncode": "Encode prompt w/ scheduling (Lazy)",
    "PCLazyLoraLoader": "Load LoRAs from prompt w/ scheduling (Lazy)",
}
