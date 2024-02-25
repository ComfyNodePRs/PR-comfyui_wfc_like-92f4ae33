from collections import defaultdict
from functools import lru_cache, cache
from multiprocessing.managers import ValueProxy
import numpy as np
import cv2 as cv
from numpy import ndarray
from py_search.base import Problem, Node
import hashlib


TILE_DIGEST_SIZE = 4  # in bytes
NP_ENCODED_TILE_TYPE = "longlong"
WORLD_DIGEST_SIZE = 4


class WFC_Sample:
    """
    From a source image, compute & store the following data:
        tile_data : { tile hashcode : ( tile , frequency )  , ... }
        super_tile_data : [ ( 3x3 matrix of tile hashes, count ) ]
        tile_dims : ( tile height, width, channels )
    """

    @staticmethod
    def tile_to_hash(tile):
        return int.from_bytes(hashlib.blake2b(tile.tobytes(), digest_size=TILE_DIGEST_SIZE).digest(), byteorder="big")

    def __init__(self, src_imgs, cell_width, cell_height):
        self.tile_data, self.super_tile_data, self.tile_dims = self.prepare(src_imgs[0], cell_width, cell_height)

        for img in src_imgs[1:]:
            r_tile_data, r_super_tile_data, _ = self.prepare(img, cell_width, cell_height)
            self.tile_data = {k: (v[0], self.tile_data.get(k, (None, 0))[1] + v[1])
                              for k, v in {**self.tile_data, **r_tile_data}.items()}
            self.super_tile_data = self.merge_tuples(self.super_tile_data, r_super_tile_data)

    @staticmethod
    def merge_tuples(list1, list2):  # adapted from GPT; might be wrong
        result_dict = defaultdict(int)
        shape = list1[0][0].shape
        for ndarray, value in list1 + list2:
            # Convert ndarray to a hashable type (tuple)
            key = tuple(ndarray.flatten())
            result_dict[key] += value

        # Convert the result back to a list of tuples
        result_list = [(np.array(key).reshape(shape), value) for key, value in result_dict.items()]
        return result_list

    def get_tile_data(self) -> dict[int, tuple[ndarray, float]]:
        """
        @return: hash to (tile, freq) pair dictionary
        """
        return self.tile_data

    def get_super_tile_data(self) -> list[tuple[ndarray, int]]:
        """
        @return: list of (super tile, count) pairs
        """
        return self.super_tile_data

    @staticmethod
    def adjust_image_to_tile_size(img, tile_height, tile_width):
        """
        @return: the adjusted image, height in number of cells, and width
        """
        if img.shape[0] % tile_height != 0:
            print(f"src height is not divisible by cell_height!")
        if img.shape[1] % tile_width != 0:
            print(f"src width is not divisible by cell_height!")

        height_in_tiles = round(img.shape[0] / tile_height)
        width_in_tiles = round(img.shape[1] / tile_width)
        new_height = tile_height * height_in_tiles
        new_width = tile_width * width_in_tiles

        adjusted_image = cv.resize(img, (new_width, new_height)) if new_height != img.shape[0] or new_width != \
                                                                    img.shape[1] else img.copy()

        return adjusted_image, height_in_tiles, width_in_tiles

    @staticmethod
    def prepare(src_img, tile_width, tile_height):
        src_shape = src_img.shape
        adjusted_img, ycell_len, xcell_len = WFC_Sample.adjust_image_to_tile_size(src_img, tile_height, tile_width)
        tiles = adjusted_img.reshape(
            (
                ycell_len,  # adjusted_img.shape[0] // tile_height,
                tile_height,
                xcell_len,  # adjusted_img.shape[1] // tile_width,
                tile_width,
                src_shape[2]
            )
        ).swapaxes(1, 2)
        size_in_tiles = tiles.shape[:2]
        tiles = tiles.reshape(-1, tile_height, tile_width, src_shape[2])
        utiles, counts = np.unique(tiles, axis=0, return_counts=True)
        ut_hashes = [WFC_Sample.tile_to_hash(tile) for tile in utiles]
        # assert len(set(ut_hashes)) == len(ut_hashes)

        tiles_data = dict(zip(ut_hashes, zip(utiles, counts / tiles.shape[0])))
        hashed_tiles = np.array([WFC_Sample.tile_to_hash(tile) for tile in tiles])
        hashed_tiles = hashed_tiles.reshape(size_in_tiles)

        super_tiles = np.array(
            [hashed_tiles[y:y + 3, x:x + 3] for y in range(size_in_tiles[0] - 2) for x in range(size_in_tiles[1] - 2)])

        u_super_tiles, super_counts = np.unique(super_tiles, axis=0, return_counts=True)
        super_tiles_data = list(zip(u_super_tiles, super_counts))

        return tiles_data, super_tiles_data, (tile_height, tile_width, src_shape[2])

    def img_to_tile_encoded_world(self, src_img):
        adjusted_img, ycell_len, xcell_len = WFC_Sample.adjust_image_to_tile_size(src_img, *self.tile_dims[:2])
        tiles = adjusted_img.reshape(
            (
                ycell_len,
                self.tile_dims[0],
                xcell_len,
                self.tile_dims[1],
                adjusted_img.shape[2]
            )
        ).swapaxes(1, 2)
        size_in_tiles = tiles.shape[:2]
        tiles = tiles.reshape(-1, self.tile_dims[0], self.tile_dims[1], adjusted_img.shape[2])
        hashed_world = [WFC_Sample.tile_to_hash(tile) for tile in tiles]
        hashed_world = np.array([tile if tile in self.tile_data.keys() else 0 for tile in hashed_world]).astype(
            NP_ENCODED_TILE_TYPE)
        return hashed_world.reshape(*size_in_tiles)

    def tile_encoded_to_img(self, src_state: ndarray):
        th, tw = self.tile_dims[:2]
        img = np.zeros((src_state.shape[0] * th, src_state.shape[1] * tw, self.tile_dims[2]))
        mask = np.ones((src_state.shape[0] * th, src_state.shape[1] * tw)) * 255
        for (y, x), hashcode in np.ndenumerate(src_state):
            if hashcode in self.tile_data:
                img[y * th:(y + 1) * th, x * tw:(x + 1) * tw] = self.tile_data[hashcode][0]
                mask[y * th:(y + 1) * th, x * tw:(x + 1) * tw] = np.zeros(self.tile_data[hashcode][0].shape[:2])
        return img, mask


class WFC_Problem(Problem):
    def __init__(self, sample: WFC_Sample, starting_state: ndarray, seed: int = 0, use_8_cardinals: bool = False,
                 max_freq_adjust: float = 1, plateau_check_interval: int = -1,
                 starting_temperature: float = 50, min_min_temperature: float = 0, max_min_temperature: float = 80,
                 reverse_depth_w: float = 1, node_cost_w: float = 1, path_entropy_average_w: float = 0,
                 stop_proxy: ValueProxy = None, ticker_proxy: ValueProxy = None
                 ):
        """
        @param sample: contains the tiles, their frequencies and "implicit" constraints
        @param starting_state: complete the provided state instead of starting with an empty world.
                                If provided, width and height are ignored.
        @param seed: used to setup the generator so that the result can be reproducible ( deterministic )
        @param use_8_cardinals: consider the surrounding 8 tiles if set to TRUE; OR only the 4 cardinals if set to FALSE
        @param max_freq_adjust: scale frequency adjustment weight.
                                if set to zero, the frequency of the tiles registered in the given samples is ignored.
        @param plateau_check_interval: the number of nodes to be processed before checking the highest registered depth.
                                       if the depth hasn't changed between checks, the search is stopped.
                                       Set to 0 (zero) to ignore plateau checks.
                                       Set to -1 (minus 1) to auto select depending on the number of nodes to process.
        @param starting_temperature:
        @param min_min_temperature:
        @param max_min_temperature:
        @param reverse_depth_w:
        @param node_cost_w:
        @param path_entropy_average_w:
        """
        super().__init__(initial=(0, 0), initial_cost=0, extra=0)
        # initial state -> (depth, hash) -> 0 represents empty world at the start, at zero depth
        # extra -> the sum of entropies of the closed nodes in the current branch ( at the moment they were closed )

        # BASIC DATA
        self._stop_proxy = stop_proxy
        self._ticker_proxy = ticker_proxy
        self.rng = np.random.default_rng(seed=seed)
        self._sample: WFC_Sample = sample

        non_zeroes = np.count_nonzero(starting_state)
        self._number_of_tiles_to_process = starting_state.size - non_zeroes
        self._world_tdims = starting_state.shape
        self._temp_world_state = starting_state.copy()
        self._starting_state = starting_state.copy() if non_zeroes > 0 else None
        # _starting_state has 2 internal uses:
        # 1. if None the center tile is set to open, otherwise the state is iterated to find the tiles at the edges
        # 2. initialize state to return instead of reverting last node actions

        # KEEP TRACK OF OPEN TILES ( yet to explore after the last closed node )
        self._temp_world_open_super_tiles: set[tuple[int, int]] = set([])

        # INFLUENCE COST WEIGHTS & FINAL NODE VALUE
        # influences the nodes' costs. high temperature lowers the influence of random noise and frequency adjustments
        self._min_temperature = starting_temperature
        self._min_min_temperature, self._max_min_temperature = min_min_temperature, max_min_temperature
        self._rev_depth_w, self._node_cost_w, self._path_ent_avg_w = reverse_depth_w, node_cost_w, path_entropy_average_w

        # STOP THE SEARCH
        self._best_node = None
        self._prev_best_depth = 0
        self._last_node = None
        self._plateau_check_ticker = 0
        self._plateau_stop_steps = self._world_tdims[0] * self._world_tdims[1] / 2.0 \
            if plateau_check_interval == -1 else plateau_check_interval

        self._stop: bool = False  # stops the search; set to True when all the tiles are filled OR a plateau is reached

        # OTHERS
        self._use_8cardinals = use_8_cardinals

        tile_data = sample.get_tile_data()
        self._tile_counts = dict(zip(tile_data.keys(), [0] * len(tile_data)))

        self._max_freq_adjust = max_freq_adjust
        t = self._number_of_tiles_to_process
        a = [[0, 0, 1], [t ** 2, t, 1], [(t / 2) ** 2, t / 2, 1]]
        b = [t, t, 0]
        self._tile_freq_adjustment_poly = np.poly1d(np.linalg.solve(a, b))

    @lru_cache(maxsize=8)
    def temp_ratio(self, node_depth: int, prior_node_depth: int):
        # TODO -> potentially something to change/customize
        depth_diff = prior_node_depth - node_depth
        depth_ratio = node_depth / self._number_of_tiles_to_process
        ratio = min(.9, (abs(depth_diff) * 3) / np.sqrt(self._number_of_tiles_to_process)) if depth_diff != 0 else \
            depth_ratio ** 2.5 / 80
        return ratio

    def get_new_temperature(self, node_depth: int, prior_node_depth: int):
        limit = self._max_min_temperature if node_depth <= prior_node_depth else self._min_min_temperature
        ratio = self.temp_ratio(node_depth, prior_node_depth)
        return limit * ratio + self._min_temperature * (1 - ratio)

    @lru_cache(maxsize=32)
    def _tile_freq_adjustment_func(self, depth):
        return self._max_freq_adjust * (1 - self._tile_freq_adjustment_poly(depth) / self._number_of_tiles_to_process)

    def update_open_nodes(self, y, x, is_reopening):
        """
        Update open nodes when updating world state
        @param is_reopening:
        @return:
        """
        # 0 => nothing; 1 => open; 2 => closed
        wost = self._temp_world_open_super_tiles

        indices_to_check = [(y - 1 + _y, x - 1 + _x) for _y in range(3) for _x in range(3) if _y != 1 or _x != 1]
        if not self._use_8cardinals:
            indices_to_check = [indices_to_check[i] for i in [1, 3, 4, 6]]
        indices_to_check = [(_y, _x) for (_y, _x) in indices_to_check
                            if 0 <= _y < self._temp_world_state.shape[0] and 0 <= _x < self._temp_world_state.shape[1]]

        if is_reopening:
            wost.add((y, x))

            for (_y, _x) in indices_to_check:
                if not wost.__contains__((_y, _x)):
                    continue

                sub_indices_to_check = [(_y - 1 + __y, _x - 1 + __x) for __y in range(3)
                                        for __x in range(3)]
                if not self._use_8cardinals:
                    sub_indices_to_check = [sub_indices_to_check[i] for i in [1, 3, 5, 7]]
                sub_indices_to_check = [(__y, __x) for (__y, __x) in sub_indices_to_check
                                        if 0 <= __y < self._temp_world_state.shape[0] and 0 <= __x <
                                        self._temp_world_state.shape[1]]

                if any(self._temp_world_state[__y, __x] != 0 for (__y, __x) in sub_indices_to_check):
                    continue  # -> position should remain open
                # otherwise -> position should be closed
                wost.remove((_y, _x))

            return

        # otherwise -> closing
        wost.remove((y, x))
        for _y, _x in indices_to_check:
            if self._temp_world_state[_y, _x] == 0:
                wost.add((_y, _x))

    def get_cell_potential_states_and_costs(self, y, x, world_state, depth) -> \
            tuple[list[bytes], ndarray | None, float | None]:
        world_indices_to_check = [(y - 1 + _y, x - 1 + _x) for _y in range(3) for _x in range(3) if _y != 1 or _x != 1]
        adjacent_states = [world_state[_y, _x] if 0 <= _y < world_state.shape[0] and 0 <= _x < world_state.shape[1]
                           else 0 for (_y, _x) in world_indices_to_check]
        tiles, probabilities = \
            self.get_cell_potential_states_8cardinals(*adjacent_states) \
                if self._use_8cardinals else \
                self.get_cell_potential_states_4cardinals(*[adjacent_states[i] for i in [1, 3, 4, 6]])

        if len(tiles) == 0:
            return [], None, None  # nothing to compute, so just return early

        # generate random weights and temperature
        rands = self.rng.random(len(probabilities))
        temp = 1 - self.rng.integers(low=int(self._min_temperature), high=100) / 100.0

        # GET tile type freq in original samples AND in current generation
        tile_data = self._sample.get_tile_data()
        sample_freqs = np.array([tile_data[t][1] for t in tiles])
        current_counts = np.array([self._tile_counts[t] for t in tiles])
        current_freqs = current_counts / max(1, depth)

        if self._max_freq_adjust == 0.0:
            adjusted_freqs_diff = np.zeros(probabilities.size)
        else:
            depth_adjustment = self._tile_freq_adjustment_func(depth)
            adjusted_freqs_diff = np.sign(sample_freqs - current_freqs) * (
                    1 - np.minimum(sample_freqs, current_freqs) / np.maximum(sample_freqs, current_freqs))
            adjusted_freqs_diff *= depth_adjustment
        # ===========================================================================
        entropy: float = - np.sum(probabilities * np.log2(probabilities)) / np.log2(len(probabilities)) if len(
            probabilities) > 1 else 0

        # TODO -> review formula... multiplication w/ entropy may be too strong for some rules;
        #           consider attenuating entropy on low temperatures or when adjusting freqs
        #         OR, consider some node to customize the costs later
        costs = (1 - np.clip(probabilities
                             + adjusted_freqs_diff * temp
                             + (rands * 2 - 1) * temp
                             , 0, 1)) * entropy

        return tiles, costs, entropy

    @cache
    def get_cell_potential_states_8cardinals(self, p1, p2, p3, p4, p6, p7, p8, p9):
        """
        the state of adjacent cells; where 0 = unknown
        [[1,2,3],
         [4,c,6],
         [7,8,9]]
        @return: list of tuples (state, prob.)
        """

        def is_possible(super_tile):
            tiles = np.array([[p1, p2, p3], [p4, 0, p6], [p7, p8, p9]])
            for (y, x), tile in np.ndenumerate(tiles):
                # print(f"super={super_tile} ;  index = {(y,x)}")
                if tile == 0:
                    continue
                if super_tile[y, x] != tile:
                    return False
            return True

        # filter all the possible super tiles
        pcs = defaultdict(int)
        for stile, count in self._sample.get_super_tile_data():
            if is_possible(stile):
                pcs[stile[1, 1]] += count

        return self.map_to_probabilities(pcs)

    @cache
    def get_cell_potential_states_4cardinals(self, p2, p4, p6, p8):
        """
        Read get_cell_potential_states_8cardinals documentation.
        This function is similar, but only takes into account 4 cardinals
        """

        def is_possible(super_tile):
            tiles = np.array([[0, p2, 0], [p4, 0, p6], [0, p8, 0]])
            for (y, x), tile in np.ndenumerate(tiles):
                if tile == 0:
                    continue
                if super_tile[y, x] != tile:
                    return False
            return True

        # filter all the possible super tiles
        pcs = defaultdict(int)
        for stile, count in self._sample.get_super_tile_data():
            if is_possible(stile):
                pcs[stile[1, 1]] += count

        return self.map_to_probabilities(pcs)

    def map_to_probabilities(self, pcs):
        if len(pcs) == 0:
            return [], []
        # compute probabilities for each possible state
        counts = list(pcs.values())
        total_counts: float = sum(counts)
        probabilities: ndarray = np.array(counts).astype(np.float32) / total_counts
        assert np.sum(probabilities > 1) == 0  # seems fine
        return pcs.keys(), probabilities

    def node_value(self, node: Node):
        """
        The function used to compute the value of a node.
        """
        rev_depth = (1 + self._number_of_tiles_to_process - node.depth())
        # can be used to prioritize nodes w/ high depth, for a quicker generation

        node_cost = node.cost()
        # if temperature is high, this is the most promising locally

        path_avg_entropy = node.extra / (1 + node.depth())
        # can be used to backtrack preemptively if path doesn't look strong
        # the entropies used are the ones obtain when closing a node, so this might be misleading indicator

        return rev_depth * self._rev_depth_w + node_cost * self._node_cost_w + path_avg_entropy * self._path_ent_avg_w

    def successors(self, node):
        from py_search.base import Node
        """
        Generate all possible next states
        """
        if (self._stop_proxy is not None and self._stop_proxy.get()) :
            #print("")
            raise InterruptedError()

        # world state is kept in self._temp_world_state; updated here when closing the node.
        # get_world_state func rollbacks any actions when depth is maintained or decreased.

        cum_entropy = 0
        if node.depth() == 0:
            self._best_node = node
            self.open_nodes_on_depth_zero()
            self._last_node = node
            world_state = self._temp_world_state
        else:
            world_state = self.get_world_state(self._last_node, node)
            self._min_temperature = self.get_new_temperature(node.depth(), self._last_node.depth())
            self._last_node = node
            cum_entropy = node.parent.extra

        depth = node.depth()
        if depth > self._best_node.depth():
            self._best_node = node
            if self._ticker_proxy is not None:
                self._ticker_proxy.set(self._ticker_proxy.get()+1)
        if depth >= self._number_of_tiles_to_process:
            self._stop = True
            print("\nEnded search with all tiles filled.")
            return

        if self._plateau_stop_steps > 0:
            self._plateau_check_ticker += 1
            if self._plateau_check_ticker >= self._plateau_stop_steps:
                if self._prev_best_depth == self._best_node.depth():
                    self._stop = True
                    print("\nEnded due to depth plateauing.")
                    if self._ticker_proxy is not None:
                        self._ticker_proxy.set(self._ticker_proxy.get() + self._number_of_tiles_to_process - self._best_node.depth())
                    return
                self._plateau_check_ticker = 0
                self._prev_best_depth = self._best_node.depth()

        iyxs = self._temp_world_open_super_tiles
        potential_collapses = [self.get_cell_potential_states_and_costs(y, x, world_state, depth) for (y, x) in iyxs]

        #print(f"depth = {depth:5,.0f}  |  temperature={self._min_temperature:5,.1f}  |  "
        #      f"freq_depth_adjustment={self._tile_freq_adjustment_func(depth):6,.2f}  |  "
        #      f"open tiles:{len(iyxs):5,.0f}    ", end="\r")

        if len(potential_collapses) == 0 or any(counts is None for (_, counts, _) in potential_collapses):
            # this state is impossible, so don't return any of its children
            node.node_cost = float("inf")  # the node has now been closed, but it could help w/ debugging
            return

        for (y, x), (potential_states, costs, entropy) in zip(iyxs, potential_collapses):
            items = zip(potential_states, costs)

            # if self._temperature > xxx:  # TODO consider making this an option set by the user
            #    # items = sorted(items, key=itemgetter(1))[:2]
            #    items = sorted(items, key=itemgetter(1))
            #    take = round(2 * self._temperature / self._max_temperature + len(items) * (
            #                1 - self._temperature / self._max_temperature))
            #    items = items[:take]

            for tile_type, cost in items:
                world_state[y, x] = tile_type
                world_state_hash = int.from_bytes(hashlib.blake2b(world_state.tobytes(), digest_size=4).digest(),
                                                  byteorder="big")
                world_state[y, x] = 0
                yield Node(state=(depth + 1, world_state_hash),
                           parent=node, action=((y, x), tile_type), node_cost=cost, extra=cum_entropy + entropy)

    def goal_test(self, state_node, goal_node=None):
        # state is not kept in each node, so the checks are done when closing a node.
        # goal_test is only defined to terminate the search;
        # the real goal test is done in the successors method
        return self._stop

    def get_world_state(self, last_node: Node, current_node: Node):
        start_depth = min(last_node.depth(), current_node.depth())

        p_node = last_node
        c_node = current_node

        for _ in range(p_node.depth() - start_depth):
            self.revert_action(p_node.action)
            p_node = p_node.parent

        for _ in range(c_node.depth() - start_depth):
            c_node = c_node.parent

        common_depth = 0
        for i in range(start_depth + 1)[::-1]:
            if p_node.state == c_node.state:
                common_depth = i
                break
            self.revert_action(p_node.action)
            p_node = p_node.parent
            c_node = c_node.parent

        # NOTE: node is removed from opened set when closing; thus, the order of operations needs to be preserved
        c_node = current_node
        nodes_to_apply = []
        for _ in range(current_node.depth() - common_depth):
            nodes_to_apply.append(c_node)
            c_node = c_node.parent

        for node in nodes_to_apply[::-1]:
            self.apply_action(node.action)

        return self._temp_world_state

    def open_nodes_on_depth_zero(self):
        if self._starting_state is None:
            # open center tile
            self._temp_world_open_super_tiles.add((self._world_tdims[0] // 2, self._world_tdims[1] // 2))
            return
        # otherwise -> find all in starting state

        relative_indices_to_check = [(_y, _x) for _y in range(3) for _x in range(3) if _y != 1 or _x != 1]
        if not self._use_8cardinals:
            relative_indices_to_check = [relative_indices_to_check[i] for i in [1, 3, 4, 6]]

        wost = self._temp_world_open_super_tiles
        for (y, x), tile in np.ndenumerate(self._starting_state):
            if tile != 0:
                continue
            indices_to_check = [(__y, __x) for (_y, _x) in relative_indices_to_check
                                if 0 <= (__y := y + _y) < self._temp_world_state.shape[0]
                                and 0 <= (__x := x + _x) < self._temp_world_state.shape[1]]

            if any(self._starting_state[__y, __x] != 0 for (__y, __x) in indices_to_check):
                wost.add((y, x))

    def revert_action(self, node_action):
        (pos, state) = node_action
        self._tile_counts[self._temp_world_state[*pos]] -= 1
        self._temp_world_state[*pos] = 0
        self.update_open_nodes(*pos, is_reopening=True)

    def apply_action(self, node_action):
        (pos, state) = node_action
        self._temp_world_state[*pos] = state
        self._tile_counts[state] += 1
        self.update_open_nodes(*pos, is_reopening=False)

    def get_solution_state(self):
        node: Node = self._best_node
        encoded_state = np.zeros(self._temp_world_state.shape[:2]) if self._starting_state is None \
            else self._starting_state.copy()
        for d in range(node.depth()):
            (y, x), tile_hash = node.action
            node = node.parent
            if tile_hash != 0:
                encoded_state[y, x] = tile_hash

        return encoded_state.astype(NP_ENCODED_TILE_TYPE)
