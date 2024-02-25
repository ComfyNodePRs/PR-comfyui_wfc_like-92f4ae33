from _operator import xor
from py_search.informed import best_first_search
from comfy import utils
from comfy.model_management import throw_exception_if_processing_interrupted, processing_interrupted
from .wcf import *


def waiting_loop(abort_loop_event, interruption_proxy: ValueProxy, pbar: utils.ProgressBar, ticker_proxy: ValueProxy, total_steps):
    """
    Listens for interrupts and propagates to Problem running using interruption_proxy.
    Updates progress_bar via ticker_proxy, updated within Problem instances.

    @param abort_loop_event: to be triggered in the main thread once the problem(s) have been solved
    @param interruption_proxy: proxy value used to cancel the search(es)
    @param pbar: comfyui progress bar to update every 100 milliseconds
    @param ticker_proxy: the max depth so far (or sum of max depths in case of many problems)
    @param total_steps: the total number of nodes to process in the problem(s)
    """
    from time import sleep
    while not abort_loop_event.is_set():
        sleep(.1)  # pause for 1 second
        if processing_interrupted():
            interruption_proxy.set(True)
            return
        pbar.update_absolute(ticker_proxy.get(), total_steps)


class WFC_SampleNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "img_batch": ("IMAGE",),
                    "tile_width": ("INT", {"default": 32, "min": 1, "max": 128}),
                    "tile_height": ("INT", {"default": 32, "min": 1, "max": 128}),
                    "output_tiles": ("BOOLEAN", {"default": False})
                },
        }

    RETURN_TYPES = ("WFC_Sample", "IMAGE",)
    RETURN_NAMES = ("sample", "unique_tiles",)
    FUNCTION = "compute"
    CATEGORY = "Bmad/WFC"

    def compute(self, img_batch, tile_width, tile_height, output_tiles):
        import torch

        samples = [np.clip(255. * img_batch[i].cpu().numpy().squeeze(), 0, 255).astype(np.uint8)
                   for i in range(img_batch.shape[0])]
        sample = WFC_Sample(samples, tile_width, tile_height)

        if output_tiles:
            tiles = [torch.from_numpy(tile.astype(np.float32) / 255.0).unsqueeze(0)
                     for tile, freq in sample.get_tile_data().values()]
            tiles = torch.concat(tiles)
        else:
            tiles = torch.empty((1, 1, 1))

        return (sample, tiles,)


class WFC_GenerateNode:
    @staticmethod
    def NODE_INPUT_TYPES():
        return {
            "required":
                {
                    "sample": ("WFC_Sample",),
                    "starting_state": ("WFC_State",),
                    "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "max_freq_adjust": ("FLOAT", {"default": 1, "min": 0, "max": 1, "step": .01}),
                    "use_8_cardinals": ("BOOLEAN", {"default": False}),
                    "plateau_check_interval": ("INT", {"default": -1, "min": -1, "max": 10000}),
                },
            "optional":
                {
                    "custom_temperature_config": ("WFC_TemperatureConfig",),
                    "custom_node_value_config": ("WFC_NodeValueConfig",)
                }
        }

    @classmethod
    def INPUT_TYPES(s):
        return s.NODE_INPUT_TYPES()

    RETURN_TYPES = ("WFC_State",)
    RETURN_NAMES = ("state", "unique_tiles",)
    FUNCTION = "compute"
    CATEGORY = "Bmad/WFC"

    def compute(self, custom_temperature_config=None, custom_node_value_config=None, **kwargs):
        from multiprocessing import Manager
        from multiprocessing.managers import ValueProxy
        from threading import Event, Thread

        if custom_temperature_config is not None:
            kwargs.update(custom_temperature_config)

        if custom_node_value_config is not None:
            kwargs.update(custom_node_value_config)

        # prepare stuff to process interrupts & update bar
        # TODO count is also done inside Problem, maybe should use as optional arg to avoid repeating the operation
        ss = kwargs["starting_state"]
        total_tiles_to_proc = ss.size-np.count_nonzero(ss)
        manager = Manager()
        stop: ValueProxy = manager.Value('b', False)  # Set to True to abort
        ticker: ValueProxy = manager.Value('i', 0)  # counts max depth increments
        kwargs.update({"stop_proxy": stop})
        kwargs.update({"ticker_proxy": ticker})
        finished_event = Event()
        pbar: utils.ProgressBar = utils.ProgressBar(total_tiles_to_proc)

        t = Thread(target=waiting_loop, args=(finished_event, stop, pbar, ticker, total_tiles_to_proc))
        t.start()

        problem = WFC_Problem(**kwargs)
        try:
            next(best_first_search(problem, graph=True))  # find 1st solution
        except InterruptedError:
            pass
        except StopIteration:
            print("Exhausted all possibilities without finding a complete solution ; or some irregularity occurred.")
        finally:
            finished_event.set()
            if stop.get():
                throw_exception_if_processing_interrupted()

        result = problem.get_solution_state()
        return (result,)


class WFC_Encode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "img": ("IMAGE",),
                    "sample": ("WFC_Sample",),
                },
        }

    RETURN_TYPES = ("WFC_State",)
    RETURN_NAMES = ("state",)
    FUNCTION = "compute"
    CATEGORY = "Bmad/WFC"

    def compute(self, img, sample: WFC_Sample):
        samples = [np.clip(255. * img[i].cpu().numpy().squeeze(), 0, 255).astype(np.uint8) for i in range(img.shape[0])]
        encoded = sample.img_to_tile_encoded_world(samples[0])  # no batch enconding, only a single image is encoded
        return (encoded,)


class WFC_Decode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "state": ("WFC_State",),
                    "sample": ("WFC_Sample",),
                },
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "compute"
    CATEGORY = "Bmad/WFC"

    def compute(self, state, sample: WFC_Sample):
        import torch

        img, mask = sample.tile_encoded_to_img(state)
        img = torch.from_numpy(img.astype(np.float32) / 255.0).unsqueeze(0)
        mask = torch.from_numpy(mask.astype(np.float32) / 255.0).unsqueeze(0)
        return (img, mask,)


class WFC_CustomTemperature:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "starting_temperature": ("INT", {"default": 50, "min": 0, "max": 99}),
                    "min_min_temperature": ("INT", {"default": 0, "min": 0, "max": 99}),
                    "max_min_temperature": ("INT", {"default": 80, "min": 0, "max": 99}),
                },
        }

    RETURN_TYPES = ("WFC_TemperatureConfig",)
    RETURN_NAMES = ("temperature",)
    FUNCTION = "send"
    CATEGORY = "Bmad/WFC"

    def send(self, **kwargs):
        return (kwargs,)


class WFC_CustomValueWeights:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "reverse_depth_w": ("FLOAT", {"default": 1, "min": 0, "max": 10, "step": .001}),
                    "node_cost_w": ("FLOAT", {"default": 1, "min": 0, "max": 10, "step": .001}),
                    "path_entropy_average_w": ("FLOAT", {"default": 0, "min": 0, "max": 10, "step": .001}),
                },
        }

    RETURN_TYPES = ("WFC_NodeValueConfig",)
    RETURN_NAMES = ("weights",)
    FUNCTION = "send"
    CATEGORY = "Bmad/WFC"

    def send(self, **kwargs):
        return (kwargs,)


class WFC_EmptyState:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "width": ("INT", {"default": 16, "min": 4, "max": 128}),
                    "height": ("INT", {"default": 16, "min": 4, "max": 128}),
                },
        }

    RETURN_TYPES = ("WFC_State",)
    RETURN_NAMES = ("state",)
    FUNCTION = "create"
    CATEGORY = "Bmad/WFC"

    def create(self, width, height):
        return (np.zeros((width, height)),)


class WFC_Filter:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required":
                {
                    "state": ("WFC_State",),
                    "tiles_batch": ("IMAGE",),
                    "invert": ("BOOLEAN", {"default": False}),
                },
        }

    RETURN_TYPES = ("WFC_State",)
    RETURN_NAMES = ("state",)
    FUNCTION = "create"
    CATEGORY = "Bmad/WFC"

    def create(self, state: ndarray, tiles_batch, invert):
        to_filter = [WFC_Sample.tile_to_hash(
            np.clip(255. * tiles_batch[i].cpu().numpy().squeeze(), 0, 255).astype(np.uint8))
            for i in range(tiles_batch.shape[0])]
        new_state = [t if xor(t in to_filter, invert) else 0 for t in state.flatten()]
        new_state = np.array(new_state).reshape(state.shape)
        return (new_state,)


def generate_single(i_kwargs):
    problem = WFC_Problem(**i_kwargs)
    try:
        next(best_first_search(problem, graph=True))  # find 1st solution
    except InterruptedError:
        return None
    except StopIteration:
        print("Exhausted all possibilities without finding a complete solution ; or some irregularity occurred.")
    result = problem.get_solution_state()
    return result


class WFC_GenParallel:
    @classmethod
    def INPUT_TYPES(s):
        gen_types = WFC_GenerateNode.NODE_INPUT_TYPES()
        gen_types["required"]["max_parallel_tasks"] = ("INT", {"default": 4, "min": 1, "max": 32})
        return gen_types

    RETURN_TYPES = ("WFC_State",)
    RETURN_NAMES = ("state",)
    FUNCTION = "gen"
    CATEGORY = "Bmad/WFC"
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True,)

    def gen(self, max_parallel_tasks, custom_temperature_config=None, custom_node_value_config=None, **kwargs):
        from multiprocessing import Manager
        from multiprocessing.managers import ValueProxy
        from joblib import Parallel, delayed
        from threading import Event, Thread

        max_parallel_tasks = max_parallel_tasks[0]
        ct_len = 0 if custom_temperature_config is None else len(custom_temperature_config)
        cnv_len = 0 if custom_node_value_config is None else len(custom_node_value_config)

        max_len = 0
        for v in kwargs.values():
            if (v_len := len(v)) > max_len:
                max_len = v_len

        # TODO count is also done inside Problem, maybe should use as optional arg to avoid repeating the operation
        ss = kwargs["starting_state"]
        total_tiles_to_proc = sum([i.size-np.count_nonzero(i) for i in ss])
        total_tiles_to_proc += (ss[-1].size-np.count_nonzero(ss[-1]))*(max_len - len(ss))

        manager = Manager()
        stop: ValueProxy = manager.Value('b', False)  # Set to True to abort
        ticker: ValueProxy = manager.Value('i', 0)  # counts max depth increments
        items = kwargs.items()
        per_gen_inputs = []
        for i in range(max_len):
            input_i = {item[0]: item[1][min(i, len(item[1]) - 1)] for item in items}
            if ct_len > 0:
                input_i.update(custom_temperature_config[min(i, ct_len - 1)])
            if cnv_len > 0:
                input_i.update(custom_node_value_config[min(i, cnv_len - 1)])
            input_i.update({"stop_proxy": stop})
            input_i.update({"ticker_proxy": ticker})
            per_gen_inputs.append(input_i)

        finished_event = Event()
        pbar: utils.ProgressBar = utils.ProgressBar(total_tiles_to_proc)
        t = Thread(target=waiting_loop, args=(finished_event, stop, pbar, ticker, total_tiles_to_proc))
        t.start()

        final_result = Parallel(n_jobs=max_parallel_tasks)(delayed(generate_single)(per_gen_inputs[i]) for i in range(max_len))

        finished_event.set()
        if stop.get():
            throw_exception_if_processing_interrupted()

        return (final_result,)
