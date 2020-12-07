import configparser as ConfigParser
import hashlib
import logging
import os
import shlex
import sys
import typing
import warnings
from functools import wraps
from io import StringIO


_logger = logging.getLogger("theano.configparser")


class TheanoConfigWarning(Warning):
    def warn(cls, message, stacklevel=0):
        warnings.warn(message, cls, stacklevel=stacklevel + 3)

    warn = classmethod(warn)


class _ChangeFlagsDecorator:
    def __init__(self, *args, _root=None, **kwargs):
        # the old API supported passing a dict as the first argument:
        args = dict(args)
        args.update(kwargs)
        self.confs = {k: _root._config_var_dict[k] for k in args}
        self.new_vals = args
        self._root = _root

    def __call__(self, f):
        @wraps(f)
        def res(*args, **kwargs):
            with self:
                return f(*args, **kwargs)

        return res

    def __enter__(self):
        self.old_vals = {}
        for k, v in self.confs.items():
            self.old_vals[k] = v.__get__(self._root, self._root.__class__)
        try:
            for k, v in self.confs.items():
                v.__set__(self._root, self.new_vals[k])
        except Exception:
            _logger.error(f"Failed to change flags for {self.confs}.")
            self.__exit__()
            raise

    def __exit__(self, *args):
        for k, v in self.confs.items():
            v.__set__(self._root, self.old_vals[k])


def _hash_from_code(msg):
    """This function was copied from theano.gof.utils to get rid of that import."""
    # hashlib.sha256() requires an object that supports buffer interface,
    # but Python 3 (unicode) strings don't.
    if isinstance(msg, str):
        msg = msg.encode()
    # Python 3 does not like module names that start with
    # a digit.
    return "m" + hashlib.sha256(msg).hexdigest()


class TheanoConfigParser:
    """ Object that holds configuration settings. """

    def __init__(self, flags_dict: dict, theano_cfg, theano_raw_cfg):
        self._flags_dict = flags_dict
        self._theano_cfg = theano_cfg
        self._theano_raw_cfg = theano_raw_cfg
        self._config_var_dict = {}
        super().__init__()

    def __str__(self, print_doc=True):
        sio = StringIO()
        self.config_print(buf=sio, print_doc=print_doc)
        return sio.getvalue()

    def config_print(self, buf, print_doc=True):
        for cv in self._config_var_dict.values():
            print(cv, file=buf)
            if print_doc:
                print("    Doc: ", cv.doc, file=buf)
            print("    Value: ", cv.__get__(self, self.__class__), file=buf)
            print("", file=buf)

    def get_config_hash(self):
        """
        Return a string sha256 of the current config options. In the past,
        it was md5.

        The string should be such that we can safely assume that two different
        config setups will lead to two different strings.

        We only take into account config options for which `in_c_key` is True.
        """
        all_opts = sorted(
            [c for c in self._config_var_dict.values() if c.in_c_key],
            key=lambda cv: cv.fullname,
        )
        return _hash_from_code(
            "\n".join(
                [
                    "{} = {}".format(cv.fullname, cv.__get__(self, self.__class__))
                    for cv in all_opts
                ]
            )
        )

    def add(self, name, doc, configparam, in_c_key=True):
        """Add a new variable to TheanoConfigParser.

        This method performs some of the work of initializing `ConfigParam` instances.

        Parameters
        ----------
        name: string
            The full name for this configuration variable. Takes the form
            ``"[section0__[section1__[etc]]]_option"``.
        doc: string
            A string that provides documentation for the config variable.
        configparam: ConfigParam
            An object for getting and setting this configuration parameter
        in_c_key: boolean
            If ``True``, then whenever this config option changes, the key
            associated to compiled C modules also changes, i.e. it may trigger a
            compilation of these modules (this compilation will only be partial if it
            turns out that the generated C code is unchanged). Set this option to False
            only if you are confident this option should not affect C code compilation.

        """
        if "." in name:
            raise ValueError(
                f"Dot-based sections were removed. Use double underscores! ({name})"
            )
        if hasattr(self, name):
            raise AttributeError(f"The name {name} is already taken")
        configparam.doc = doc
        configparam.fullname = name
        configparam.in_c_key = in_c_key
        # Trigger a read of the value from config files and env vars
        # This allow to filter wrong value from the user.
        if not callable(configparam.default):
            configparam.__get__(self, type(self), delete_key=True)
        else:
            # We do not want to evaluate now the default value
            # when it is a callable.
            try:
                self.fetch_val_for_key(name)
                # The user provided a value, filter it now.
                configparam.__get__(self, type(self), delete_key=True)
            except KeyError:
                _logger.error(
                    f"Suppressed KeyError in AddConfigVar for parameter '{name}'!"
                )

        # the ConfigParam implements __get__/__set__, enabling us to create a property:
        setattr(self.__class__, name, configparam)
        # keep the ConfigParam object in a dictionary:
        self._config_var_dict[name] = configparam

    def fetch_val_for_key(self, key, delete_key=False):
        """Return the overriding config value for a key.
        A successful search returns a string value.
        An unsuccessful search raises a KeyError

        The (decreasing) priority order is:
        - THEANO_FLAGS
        - ~./theanorc

        """

        # first try to find it in the FLAGS
        if key in self._flags_dict:
            if delete_key:
                return self._flags_dict.pop(key)
            return self._flags_dict[key]

        # next try to find it in the config file

        # config file keys can be of form option, or section__option
        key_tokens = key.rsplit("__", 1)
        if len(key_tokens) > 2:
            raise KeyError(key)

        if len(key_tokens) == 2:
            section, option = key_tokens
        else:
            section, option = "global", key
        try:
            try:
                return self._theano_cfg.get(section, option)
            except ConfigParser.InterpolationError:
                return self._theano_raw_cfg.get(section, option)
        except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
            raise KeyError(key)

    def change_flags(self, *args, **kwargs) -> _ChangeFlagsDecorator:
        """
        Use this as a decorator or context manager to change the value of
        Theano config variables.

        Useful during tests.
        """
        return _ChangeFlagsDecorator(*args, _root=self, **kwargs)


class ConfigParam:
    """Base class of all kinds of configuration parameters.

    A ConfigParam has not only default values and configurable mutability, but
    also documentation text, as well as filtering and validation routines
    that can be context-dependent.

    This class implements __get__ and __set__ methods to eventually become
    a property on an instance of TheanoConfigParser.
    """

    def __init__(
        self,
        default: typing.Union[object, typing.Callable[[object], object]],
        *,
        apply: typing.Optional[typing.Callable[[object], object]] = None,
        validate: typing.Optional[typing.Callable[[object], bool]] = None,
        mutable: bool = True,
    ):
        """
        Represents a configuration parameter and its associated casting and validation logic.

        Parameters
        ----------
        default : object or callable
            A default value, or function that returns a default value for this parameter.
        apply : callable, optional
            Callable that applies a modification to an input value during assignment.
            Typical use cases: type casting or expansion of '~' to user home directory.
        validate : callable, optional
            A callable that validates the parameter value during assignment.
            It may raise an (informative!) exception itself, or simply return True/False.
            For example to check the availability of a path, device or to restrict a float into a range.
        mutable : bool
            If mutable is False, the value of this config settings can not be changed at runtime.
        """
        self._default = default
        self._apply = apply
        self._validate = validate
        self._mutable = mutable
        self.is_default = True
        # set by TheanoConfigParser.add:
        self.fullname = None
        self.doc = None
        self.in_c_key = None

        # Note that we do not call `self.filter` on the default value: this
        # will be done automatically in AddConfigVar, potentially with a
        # more appropriate user-provided default value.
        # Calling `filter` here may actually be harmful if the default value is
        # invalid and causes a crash or has unwanted side effects.
        super().__init__()

    @property
    def default(self):
        return self._default

    @property
    def mutable(self) -> bool:
        return self._mutable

    def apply(self, value):
        """Applies modifications to a parameter value during assignment.

        Typical use cases are casting or the subsitution of '~' with the user home directory.
        """
        if callable(self._apply):
            return self._apply(value)
        return value

    def validate(self, value) -> None:
        """Validates that a parameter values falls into a supported set or range.

        Raises
        ------
        ValueError
            when the validation turns out negative
        """
        if not callable(self._validate):
            return True
        if self._validate(value) is False:
            raise ValueError(
                f"Invalid value ({value}) for configuration variable '{self.fullname}'."
            )
        return True

    def __get__(self, cls, type_, delete_key=False):
        if cls is None:
            return self
        if not hasattr(self, "val"):
            try:
                val_str = cls.fetch_val_for_key(self.fullname, delete_key=delete_key)
                self.is_default = False
            except KeyError:
                if callable(self.default):
                    val_str = self.default()
                else:
                    val_str = self.default
            self.__set__(cls, val_str)
        return self.val

    def __set__(self, cls, val):
        if not self.mutable and hasattr(self, "val"):
            raise Exception(
                "Can't change the value of {self.fullname} config parameter after initialization!"
            )
        applied = self.apply(val)
        self.validate(applied)
        self.val = applied


class EnumStr(ConfigParam):
    def __init__(
        self, default: str, options: typing.Sequence[str], validate=None, mutable=True
    ):
        """Creates a str-based parameter that takes a predefined set of options.

        Parameters
        ----------
        default : str
            The default setting.
        options : sequence
            Further str values that the parameter may take.
            May, but does not need to include the default.
        validate : callable
            See `ConfigParam`.
        mutable : callable
            See `ConfigParam`.
        """
        self.all = {default, *options}

        # All options should be strings
        for val in self.all:
            if not isinstance(val, str):
                raise ValueError(f"Non-str value '{val}' for an EnumStr parameter.")
        super().__init__(default, apply=self._apply, validate=validate, mutable=mutable)

    def _apply(self, val):
        if val in self.all:
            return val
        else:
            raise ValueError(
                f"Invalid value ('{val}') for configuration variable '{self.fullname}'. "
                f"Valid options are {self.all}"
            )

    def __str__(self):
        return f"{self.fullname} ({self.all}) "


class TypedParam(ConfigParam):
    def __str__(self):
        # The "_apply" callable is the type itself.
        return f"{self.fullname} ({self._apply}) "


class StrParam(TypedParam):
    def __init__(self, default, validate=None, mutable=True):
        super().__init__(default, apply=str, validate=validate, mutable=mutable)


class IntParam(TypedParam):
    def __init__(self, default, validate=None, mutable=True):
        super().__init__(default, apply=int, validate=validate, mutable=mutable)


class FloatParam(TypedParam):
    def __init__(self, default, validate=None, mutable=True):
        super().__init__(default, apply=float, validate=validate, mutable=mutable)


class BoolParam(TypedParam):
    """A boolean parameter that may be initialized from any of the following:
    False, 0, "false", "False", "0"
    True, 1, "true", "True", "1"
    """

    def __init__(self, default, validate=None, mutable=True):
        super().__init__(default, apply=self._apply, validate=validate, mutable=mutable)

    def _apply(self, value):
        if value in {False, 0, "false", "False", "0"}:
            return False
        elif value in {True, 1, "true", "True", "1"}:
            return True
        raise ValueError(
            f"Invalid value ({value}) for configuration variable '{self.fullname}'."
        )


class DeviceParam(ConfigParam):
    def __init__(self, default, *options, **kwargs):
        super().__init__(
            default, apply=self._apply, mutable=kwargs.get("mutable", True)
        )

    def _apply(self, val):
        if val == self.default or val.startswith("opencl") or val.startswith("cuda"):
            return val
        elif val.startswith("gpu"):
            raise ValueError(
                "You are tring to use the old GPU back-end. "
                "It was removed from Theano. Use device=cuda* now. "
                "See https://github.com/Theano/Theano/wiki/Converting-to-the-new-gpu-back-end%28gpuarray%29 "
                "for more information."
            )
        else:
            raise ValueError(
                'Invalid value ("{val}") for configuration '
                'variable "{self.fullname}". Valid options start with '
                'one of "cpu", "opencl" or "cuda".'
            )

    def __str__(self):
        return f"{self.fullname} ({self.default}, opencl*, cuda*) "


class ContextsParam(ConfigParam):
    def __init__(self):
        super().__init__("", apply=self._apply, mutable=False)

    def _apply(self, val):
        if val == "":
            return val
        for v in val.split(";"):
            s = v.split("->")
            if len(s) != 2:
                raise ValueError(f"Malformed context map: {v}")
            if s[0] == "cpu" or s[0].startswith("cuda") or s[0].startswith("opencl"):
                raise ValueError(f"Cannot use {s[0]} as context name")
        return val


def parse_config_string(config_string, issue_warnings=True):
    """
    Parses a config string (comma-separated key=value components) into a dict.
    """
    config_dict = {}
    my_splitter = shlex.shlex(config_string, posix=True)
    my_splitter.whitespace = ","
    my_splitter.whitespace_split = True
    for kv_pair in my_splitter:
        kv_pair = kv_pair.strip()
        if not kv_pair:
            continue
        kv_tuple = kv_pair.split("=", 1)
        if len(kv_tuple) == 1:
            if issue_warnings:
                TheanoConfigWarning.warn(
                    f"Config key '{kv_tuple[0]}' has no value, ignoring it",
                    stacklevel=1,
                )
        else:
            k, v = kv_tuple
            # subsequent values for k will override earlier ones
            config_dict[k] = v
    return config_dict


def config_files_from_theanorc():
    """
    THEANORC can contain a colon-delimited list of config files, like
    THEANORC=~lisa/.theanorc:~/.theanorc
    In that case, definitions in files on the right (here, ~/.theanorc) have
    precedence over those in files on the left.
    """
    rval = [
        os.path.expanduser(s)
        for s in os.getenv("THEANORC", "~/.theanorc").split(os.pathsep)
    ]
    if os.getenv("THEANORC") is None and sys.platform == "win32":
        # to don't need to change the filename and make it open easily
        rval.append(os.path.expanduser("~/.theanorc.txt"))
    return rval


def _create_default_config():
    # The THEANO_FLAGS environment variable should be a list of comma-separated
    # [section__]option=value entries. If the section part is omitted, there should
    # be only one section that contains the given option.
    THEANO_FLAGS = os.getenv("THEANO_FLAGS", "")
    THEANO_FLAGS_DICT = parse_config_string(THEANO_FLAGS, issue_warnings=True)

    config_files = config_files_from_theanorc()
    theano_cfg = ConfigParser.ConfigParser(
        {
            "USER": os.getenv("USER", os.path.split(os.path.expanduser("~"))[-1]),
            "LSCRATCH": os.getenv("LSCRATCH", ""),
            "TMPDIR": os.getenv("TMPDIR", ""),
            "TEMP": os.getenv("TEMP", ""),
            "TMP": os.getenv("TMP", ""),
            "PID": str(os.getpid()),
        }
    )
    theano_cfg.read(config_files)
    # Having a raw version of the config around as well enables us to pass
    # through config values that contain format strings.
    # The time required to parse the config twice is negligible.
    theano_raw_cfg = ConfigParser.RawConfigParser()
    theano_raw_cfg.read(config_files)

    # Instances of TheanoConfigParser can have independent current values!
    # But because the properties are assinged to the type, their existence is global.
    config = TheanoConfigParser(
        flags_dict=THEANO_FLAGS_DICT,
        theano_cfg=theano_cfg,
        theano_raw_cfg=theano_raw_cfg,
    )
    return config


config = _create_default_config()
# aliasing for old API
AddConfigVar = config.add
change_flags = config.change_flags
_config_print = config.config_print
