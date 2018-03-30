import inspect
import sys
import time


DEFAULT_TIMEOUT = 10
CURRENT_PROTOCOL_VERSION = '1'
PROTOCOL_INDEX, CREATION_INDEX, EXPIRY_INDEX, ETAG_INDEX, VALUE_INDEX = list(range(5))


if sys.version_info[0] >= 3:
    getargspec = lambda func: inspect.getfullargspec(func)[:4]
else:
    getargspec = inspect.getargspec


class Memoizer(object):
    """Cache and memoizer."""

    def __init__(self, store, **kwargs):
        kwargs['store'] = store
        self.regions = dict(default=kwargs)

    def _expand_opts(self, key, opts):
        region = None
        while region != 'default':

            # We look in the original opts (ie. specific to this function call)
            # for the region to start out in.
            if region is None:
                region = opts.get('region', 'default')

            # We keep looking at the parent of the current region, simulating
            # an inheritance chain.
            else:
                region = self.regions[region].get('parent', 'default')

            # Apply the region settings to the options.
            for k, v in self.regions[region].items():
                opts.setdefault(k, v)

        namespace = opts.get('namespace')
        if namespace:
            key = '%s:%s' % (namespace, key)

        store = opts['store']
        return key, store

    def _has_expired(self, data, opts):
        protocol, creation, old_expiry, old_etag, value = data
        assert protocol == CURRENT_PROTOCOL_VERSION, 'wrong protocol version: %r' % protocol

        current_time = time.time()

        # This one is obvious...
        if old_expiry and old_expiry < current_time:
            return True

        # It is expired if an etag has been set and provided, but they don't
        # match.
        etag = opts.get('etag')
        if etag is not None and etag != old_etag:
            return True

        # The new expiry time is too old. This seems odd to do... Oh well.
        expiry = opts.get('expiry')
        if expiry and expiry < current_time:
            return True

        # See if the creation time is too long ago for a given max_age.
        max_age = opts.get('max_age')
        if max_age is not None and (creation + max_age) < current_time:
            return True

    def get(self, key, func=None, args=(), kwargs=None, **opts):
        """Manually retrieve a value from the cache, calculating as needed.

        Params:
            key -> string to store/retrieve value from.
            func -> callable to generate value if it does not exist, or has
                expired.
            args -> positional arguments to call the function with.
            kwargs -> keyword arguments to call the function with.

        Keyword Params (options):
            These will be combined with region values (as selected by the
            "region" keyword argument, and then selected by "parent" values
            of those regions all the way up the chain to the "default" region).

            namespace -> string prefix to apply to the key before get/set.
            lock -> lock constructor. See README.
            expiry -> float unix expiration time.
            max_age -> float number of seconds until the value expires. Only
                provide expiry OR max_age, not both.

        """
        kwargs = kwargs or {}
        key, store = self._expand_opts(key, opts)

        # Create a dynamic etag.
        if opts.get('etag') is None and opts.get('etagger'):
            opts['etag'] = opts['etagger'](*args, **kwargs)

        if not isinstance(key, str):
            raise TypeError('non-string key of type %s' % type(key))

        data = store.get(key)
        if data is not None:
            if not self._has_expired(data, opts):
                return data[VALUE_INDEX]

        if func is None:
            return None

        # Prioritize passed options over a store's native lock.
        lock_func = opts.get('lock') or getattr(store, 'lock', None)
        lock = lock_func and lock_func(key)
        locked = lock and lock.acquire(opts.get('timeout', DEFAULT_TIMEOUT))

        try:
            value = func(*args, **kwargs)
        finally:
            if locked:
                lock.release()

        creation = time.time()
        expiry = opts.get('expiry')
        max_age = opts.get('max_age')
        if max_age is not None:
            expiry = min(x for x in (expiry, creation + max_age) if x is not None)

        # Need to be careful as this is the only place where we do not use the
        # lovely index constants.
        store[key] = (CURRENT_PROTOCOL_VERSION, creation, expiry, opts.get('etag'), value)

        return value

    def delete(self, key, **opts):
        """Remove a key from the cache."""
        key, store = self._expand_opts(key, opts)
        try:
            del store[key]
        except KeyError:
            pass

    def expire_at(self, key, expiry, **opts):
        """Set the explicit unix expiry time of a key."""
        key, store = self._expand_opts(key, opts)
        data = store.get(key)
        if data is not None:
            data = list(data)
            data[EXPIRY_INDEX] = expiry
            store[key] = tuple(data)
        else:
            raise KeyError(key)

    def expire(self, key, max_age, **opts):
        """Set the maximum age of a given key, in seconds."""
        self.expire_at(key, time.time() + max_age, **opts)

    def ttl(self, key, **opts):
        """Get the time-to-live of a given key; None if not set."""
        key, store = self._expand_opts(key, opts)
        if hasattr(store, 'ttl'):
            return store.ttl(key)
        data = store.get(key)
        if data is None:
            return None
        expiry = data[EXPIRY_INDEX]
        if expiry is not None:
            return max(0, expiry - time.time()) or None

    def etag(self, key, **opts):
        key, store = self._expand_opts(key, opts)
        data = store.get(key)
        return data and data[ETAG_INDEX]

    def exists(self, key, **opts):
        """Return if a key exists in the cache."""
        key, store = self._expand_opts(key, opts)
        data = store.get(key)
        # Note that we do not actually delete the thing here as the max_age
        # just for this call may have triggered a False.
        if not data or self._has_expired(data, opts):
            return False
        return True

    def __call__(self, *args, **opts):
        """A decorator to wrap around a function."""
        if args and hasattr(args[0], '__call__'):
            func = args[0]
            args = args[1:]
        else:
            # Build the decorator.
            return lambda func: self(func, *args, **opts)

        master_key = ','.join(map(repr, args)) if args else None
        return MemoizedFunction(self, func, master_key, opts)


class MemoizedFunction(object):

    def __init__(self, cache, func, master_key, opts, args=None, kwargs=None):
        self.cache = cache
        self.func = func
        self.master_key = master_key
        self.opts = opts
        self.args = args or ()
        self.kwargs = kwargs or {}

    def __get__(self, obj, owner=None):
        if obj is not None:
            return self.bind(obj)
        else:
            return self

    def __repr__(self):
        return '<%s of %s via %s>' % (self.__class__.__name__, self.func, self.cache)

    def bind(self, *args, **kwargs):
        args, kwargs = self._expand_args(args, kwargs)
        return self.__class__(
            self.cache,
            self.func,
            self.master_key,
            self.opts,
            args,
            kwargs,
        )

    def _expand_args(self, args, new_kwargs):
        args = self.args + args
        kwargs = self.kwargs.copy()
        kwargs.update(new_kwargs or {})
        return args, kwargs

    def _expand_opts(self, opts):
        for k, v in self.opts.items():
            opts.setdefault(k, v)

    def key(self, args=(), kwargs=None):

        # We need to normalize the signature of the function. This is only
        # really possible if we wrap the "real" function.
        kwargs = kwargs or {}
        spec_args, _, _, spec_defaults = getargspec(self.func)

        # Insert kwargs into the args list by name.
        orig_args = list(args)
        args = []
        for i, name in enumerate(spec_args):
            if name in kwargs:
                args.append(kwargs.pop(name))
            elif orig_args:
                args.append(orig_args.pop(0))
            else:
                break

        args.extend(orig_args)

        # Add on as many defaults as we need to.
        if spec_defaults:
            offset = len(spec_args) - len(spec_defaults)
            args.extend(spec_defaults[len(args) - offset:])

        arg_str_chunks = list(map(repr, args))
        for pair in kwargs.items():
            arg_str_chunks.append('%s=%r' % pair)
        arg_str = ', '.join(arg_str_chunks)

        key = '%s.%s(%s)' % (self.func.__module__, self.func.__name__, arg_str)
        return self.master_key + ':' + key if self.master_key else key

    def __call__(self, *args, **kwargs):
        args, copy_kwargs = self._expand_args(args, kwargs)
        return self.cache.get(self.key(args, copy_kwargs), self.func, args, kwargs, **self.opts)

    def get(self, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        return self.cache.get(self.key(args, kwargs), self.func, args, kwargs, **opts)

    def delete(self, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        self.cache.delete(self.key(args, kwargs))

    def expire(self, max_age, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        self.cache.expire(self.key(args, kwargs), max_age)

    def expire_at(self, max_age, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        self.cache.expire_at(self.key(args, kwargs), max_age)

    def ttl(self, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        return self.cache.ttl(self.key(args, kwargs))

    def exists(self, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        return self.cache.exists(self.key(args, kwargs))

    def etag(self, args=(), kwargs=None, **opts):
        args, kwargs = self._expand_args(args, kwargs)
        self._expand_opts(opts)
        return self.cache.etag(self.key(args, kwargs))
