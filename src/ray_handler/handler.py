from __future__ import annotations

import os

import typing
from collections.abc import Callable, Iterable, Iterator

P = typing.ParamSpec("P")
T = typing.TypeVar("T")

import math
import numbers
import numpy as np

from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from configparser import ConfigParser

import hashlib
import pandas as pd

import time
from tqdm import tqdm as Progressbar

import ray

from .stages import Stage


def subset_dictionary(names: Iterable, dictionary: dict) -> dict:
    import itertools

    """Get the subset of a dictionary, `dictionary`, with elements, `names`"""

    return {name: dictionary[name] for name in names}


def ensure_ray_initialized(
    func: Callable[typing.Concatenate[Handler, P], T]
) -> Callable[typing.Concatenate[Handler, P], T]:
    """Decorator for Handler methods to ensure ray server is initialized"""

    def wrapper(handler: Handler, *args: P.args, **kwargs: P.kwargs) -> T:
        if not handler.ray_is_initialized:
            if handler.address:
                # connect to existing server at address
                ray.init(address=handler.address)
            else:
                # create new server
                ray.init()

            handler.ray_is_initialized = True

        return func(handler, *args, **kwargs)

    return wrapper


class Handler:
    """Handler class

    Only main handler class interfaces with ray.

    Parameters
    ----------
    default_parameters : dict
        Dictionary of default parameters that determine the output of
        primary function evaluations of the stages of a script.
        Each item has signature `name: (value, description)`, corresponding
        to a parameter with name, `name`, default value, `value`, and
        description, `description`.
        Parameter type is inferred from the type of `value`, and no default
        value is given if `value` is a type (this must be provided by the
        user).
    default_options : dict
        Dictionary of default options that do not affect the output of
        primary function evaluations of the stages of a script, but
        affect how they are calculated or plotted.
        See `default_parameters` for description of items.
    stages : Iterable
        Iterable of instantiated script stages in order of evaluation, whose
        class inherits from the base class `Stage`.

    """

    script_arguments_prefix: str = "-"
    """Prefix for script arguments (parameters and options)"""

    handler_arguments_prefix: str = "--"
    """Prefix for handler-specific arguments"""

    _default_handler_options: dict = dict(
        address=(
            "",
            "Address of existing ray server to connect to "
            '(automatically determined if address="auto"), '
            'or create new server if address="".',
        ),
        num_actors=(1, "Number of actors"),
        num_cpus=(1, "Number of cpus per actor"),
        t=(300.0, "Minimum saving period [s]"),
        cs=(1, "Chunksize"),
        max_nbytes=(
            10_240,
            "Maximum size of numpy arrays before they are shared "
            "between handleres [B]",
        ),
    )
    """Default handler-specific options

    Dictionary of default options that determine the behavior of the 
    handler when running the stages of a script.

    Returns
    -------
    dict
        Each item has signature `name: (value, description)`, corresponding
        to a parameter with name, `name`, default value, `value`, and
        description, `description`.
        Parameter type is inferred from the type of `value`, and no default
        value is given if `value` is a type (this must be provided by the
        user).

    """

    address: str
    """Address of existing ray server to connect to 
    (automatically determined if address="auto"), 
    or create new server if address=""."""
    num_actors: int
    """Number of actors"""
    num_cpus: int
    """Number of cpus per actor"""
    t: float
    """Minimum saving period [s]"""
    cs: float
    """Chunksize"""
    max_nbytes: int
    """Maximum size of numpy arrays before they are shared between 
    handleres [B]"""

    kwargs: dict
    """Dictionary of all parameters."""
    handler_kwargs: dict
    """Dictionary of parameters that determine the behavior of the handler 
    when running the stages of a script."""
    parameter_kwargs: dict
    """Dictionary of parameters that determine the output of primary function 
    evaluations of the stages of a script."""
    options_kwargs: dict
    """Dictionary of default options that do not affect the output of primary 
    function evaluations of the stages of a script, but affect how they 
    are calculated or plotted."""

    md5: str
    """md5 hash of arguments gives unique ID"""
    data_directory: str
    """Directory of data files (equal to `md5`)"""
    progress_file: str
    """Location of progress file"""
    binary_file: str
    """Location of data binary file"""

    progress_frame: pd.DataFrame
    """Pandas dataframe representing the stages.

    Columns
    -------

    Name : str
        Used as the stage index.
    Description : str
    Progress : int
    Total : int
    
    """

    files: dict
    """Dictionary of files to be operated on by stage objects"""

    stages: tuple[type[Stage]]
    """Stages of script"""

    ray_is_initialized: bool = False
    """Whether the ray server has been initialized"""

    def __init__(
        self,
        default_parameters: dict,
        default_options: dict,
        stages: Iterable[type[Stage]],
    ):
        self.stages = tuple(stages)

        uninstantiated_stage_names = [
            stage.__name__ for stage in self.stages if isinstance(stage, type)
        ]
        if uninstantiated_stage_names:
            raise TypeError(f"uninstantiated stages: {uninstantiated_stage_names}")

        stage_names = tuple(stage.name for stage in self.stages)
        if len(set(stage_names)) < len(stage_names):
            raise ValueError("stage names must be different")

        self.kwargs = self.get_kwargs_from_command_line(
            default_parameters, default_options
        )

        self.handler_kwargs = subset_dictionary(
            self._default_handler_options, self.kwargs
        )
        self.__dict__.update(self.handler_kwargs)

        self.parameters_kwargs = subset_dictionary(default_parameters, self.kwargs)
        self.options_kwargs = subset_dictionary(default_options, self.kwargs)
        for stage in self.stages:
            stage.update(**self.parameters_kwargs, **self.options_kwargs)

        # directory name is md5 hash of parameters
        self.md5 = hashlib.md5(
            " ".join(
                str(value) for name, value in sorted(self.parameters_kwargs.items())
            ).encode()
        ).hexdigest()
        self.data_directory = self.md5

        self.progress_file = f"{self.data_directory}/progress.csv"
        if os.path.exists(self.progress_file):
            self.progress_frame = pd.read_csv(self.progress_file, index_col="Name")
        else:
            self.progress_frame = pd.DataFrame(
                [
                    [stage.name, stage.description, 0, stage.total]
                    for stage in self.stages
                ],
                columns=["Name", "Description", "Progress", "Total"],
            )
            self.progress_frame.set_index("Name", inplace=True)

        self.binary_file = f"{self.data_directory}/data.npz"

    def get_kwargs_from_command_line(self, default_parameters, default_options) -> dict:
        """Get the parameters from the command line.

        Parameters
        ----------
        default_parameters : dict
            Dictionary of default parameters that determine the output of
            primary function evaluations of the stages of a script.
            Each item has signature `name: (value, description)`, corresponding
            to a parameter with name, `name`, default value, `value`, and
            description, `description`.
            Parameter type is inferred from the type of `value`, and no default
            value is given if `value` is a type (this must be provided by the
            user).
        default_options : dict
            Dictionary of default options that do not affect the output of
            primary function evaluations of the stages of a script, but
            affect how they are calculated or plotted.
            See `default_parameters` for description of items.

        """

        argument_parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)

        for group_name, parameters, prefix in [
            ["Parameters", default_parameters, self.script_arguments_prefix],
            ["Options", default_options, self.script_arguments_prefix],
            [
                "Handler-specific options",
                self._default_handler_options,
                self.handler_arguments_prefix,
            ],
        ]:
            group = argument_parser.add_argument_group(group_name)

            for name, (value, description) in parameters.items():
                if isinstance(value, type):
                    group.add_argument(
                        f"{prefix}{name}", type=value, required=True, help=description
                    )
                elif isinstance(value, bool):
                    group.add_argument(
                        f"{prefix}{name}", action="store_true", help=description
                    )
                else:
                    group.add_argument(
                        f"{prefix}{name}",
                        type=type(value),
                        default=repr(value),
                        help=description,
                    )

        return vars(argument_parser.parse_args())

    def keep_local(self, value) -> bool:
        """Whether to keep variable locally (returns True)
        or put in object store (returns False)"""

        return (
            not isinstance(value, np.ndarray)
            or value.dtype == object  # only builtins can be pickled efficiently
            or value.nbytes <= self.max_nbytes
        )

    def run(self):
        """Run distributed handler"""

        print(f"md5: {self.md5}")

        if not os.path.exists(self.data_directory):
            os.mkdir(self.data_directory)

        if not os.path.exists(self.progress_file):
            self.progress_frame.to_csv(self.progress_file)

        config_file = f"{self.data_directory}/config.ini"
        if not os.path.exists(config_file):
            config_parser = ConfigParser()
            config_parser["Parameters"] = self.parameters_kwargs
            config_parser["Options"] = self.options_kwargs
            with open(config_file, "w") as f:
                config_parser.write(f)

        if os.path.exists(self.binary_file):
            with np.load(self.binary_file) as f:
                self.files = dict(f)
        else:
            self.files = {}

        for stage in self.stages:
            if self.get_progress(stage.name) == self.get_total(stage.name):
                pass
            elif issubclass(stage.__class__, Stage):
                if stage.description:
                    print(stage.description)
                else:
                    print(stage.name)
                stage.run(self)
            else:
                raise TypeError(
                    f"{stage.__class__.__name__} ({stage.name}) is "
                    "not a subclass of Stage"
                )

    def get_progress(self, name: str) -> int:
        """Get progress of stage"""

        return self.progress_frame.loc[name, "Progress"]

    def set_progress(self, name: str, progress: int):
        """Set progress of stage"""

        if not isinstance(progress, numbers.Integral):
            raise TypeError("progress must be an integer")
        elif progress < 0:
            raise ValueError("progress must be non-negative")
        else:
            self.progress_frame.loc[name, "Progress"] = progress

    def get_total(self, name: str) -> int:
        """Get total number of iterations of stage"""

        return self.progress_frame.loc[name, "Total"]

    def set_total(self, name: str, total: int):
        """Set total number of iterations of stage"""

        if not isinstance(total, numbers.Integral):
            raise TypeError("total must be an integer")
        else:
            self.progress_frame.loc[name, "Total"] = total

    @ensure_ray_initialized
    def _make_actor_kwargs(self, stage: type[Stage]) -> dict:
        """Create dictionary of keyword arguments for actors (remote stage)

        Attributes with `keep_local=False` are put in the object store and
        passed by reference.

        """

        return {
            name: value if self.keep_local(value) else ray.put(value)
            for name, value in stage.kwargs.items()
        }

    @ensure_ray_initialized
    def _make_actor_pool(
        self, stage_cls: typing.Type[Stage], **actor_kwargs
    ) -> ray.util.ActorPool:
        """Make a pool of actors for a given stage class, stage_cls, with
        keyword arguments, actor_kwargs"""

        @ray.remote(num_cpus=self.num_cpus)
        class Actor(stage_cls):
            """It is necessary to create an inherited class to use the
            ray.remote decorator with arguments, since
            ray.remote(cls, num_cpus=self.num_cpus) does not work"""

        actors = tuple(Actor.remote(**actor_kwargs) for _ in range(self.num_actors))

        return ray.util.ActorPool(actors)

    def _evaluate_unordered(
        self,
        stage: type[Stage],
        get_func: Callable[[type[Stage]], Callable[[typing.Any], typing.Any]],
        iterable: Iterable,
    ) -> Iterator:
        """Evaluate a stage's primary function.

        Evaluates the primary function, `func`, of a stage, `stage`.

        Parameters
        ----------
        stage : Stage
            Initialized stage
        get_func : callable
            Function, with signature `get_func(stage) -> func`, that
            returns the stage's primary function, `func` with signature
            `func(x) -> y`, that operates on each input, `x`, and returns a
            result, `y`.
        iterable
            Iterable of inputs, `x`.

        Yields
        ------
        y

        warning::
            Results are yielded in completion order, not
            submission order. If order is important, then include this in the
            return value of `func`.

        """

        if self.num_actors < 1:
            raise ValueError("number of actors must be positive")
        elif self.num_actors == 1:
            # serialized evaluation

            func = get_func(stage)

            yield from map(func, iterable)
        else:
            # parallel evaluation

            actor_kwargs = self._make_actor_kwargs(stage)

            actor_pool = self._make_actor_pool(stage.__class__, **actor_kwargs)

            def pool_func(actor, x):
                return get_func(actor).remote(x)

            yield from actor_pool.map_unordered(pool_func, iterable)

    def evaluate_in_unordered_chunks(
        self,
        stage: type[Stage],
        get_func: Callable[[type[Stage]], Callable[[typing.Any], typing.Any]],
        iterable: Iterable,
        total: typing.Union[int, None] = None,
    ) -> Iterator[list]:
        """Evaluate a stage's primary function.

        Evaluates the primary function, `func`, of a stage, `stage`.

        - If `num_actors==1`, then evaluates serially.

        - If `num_actors>1`, then evaluates in parallel with a pool of
        actors.

        Chunks of output in completion order are yielded every time period,
        `t`, or when evaluation is complete.

        Parameters
        ----------
        stage : Stage
            Initialized stage
        get_func : callable
            Function, with signature `get_func(stage) -> func`, that
            returns the stage's primary function, `func` with signature
            `func(x) -> y`, that operates on each input, `x`, and returns a
            result, `y`.
        iterable
            Iterable of inputs, `x`.
        total : {int, None}, optional
            Length of iterable. If `total = None`, then the iterable is of
            indeterminate length. (default is `None`)

        Yields
        ------
        chunk : list
            List of newly completed output, `y`.

        warning::
            Results are returned in chunks in completion order, not
            submission order. If order is important, then include this in the
            return value of `func`.

        """

        if isinstance(stage, type):
            raise TypeError("stage must be instantiated")
        elif not issubclass(stage.__class__, Stage):
            raise TypeError("stage must be subclass of Stage")

        if total is None:
            pass
        elif not isinstance(total, numbers.Integral):
            raise TypeError("total must be an integer")
        elif total < 0:
            raise ValueError("total must be positive")

        next_time = time.perf_counter() + self.t
        chunk = []

        for y in Progressbar(
            self._evaluate_unordered(stage, get_func, iterable), total=total
        ):
            chunk.append(y)

            if time.perf_counter() > next_time:
                yield chunk

                next_time = time.perf_counter() + self.t
                chunk = []

        if chunk:
            yield chunk

    def optimized_chunksize(self, size: int) -> int:
        """Optimized chunksize for an iterable, of length `size`"""

        return min(math.floor(size / (3 * self.num_actors)), self.cs)

    def save(self):
        """Save files to npz binary and progress dataframe to csv file"""

        np.savez_compressed(self.binary_file, **self.files)
        self.progress_frame.to_csv(self.progress_file)
