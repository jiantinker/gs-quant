"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import builtins
from concurrent.futures import Future
import copy
import datetime as dt
import dateutil
from enum import EnumMeta
import inflection
from inspect import signature, Parameter
import keyword
import logging
import pandas as pd
from typing import Mapping, Optional, Tuple, Union, get_type_hints


_logger = logging.getLogger(__name__)


def _normalise_arg(arg: str) -> str:
    if keyword.iskeyword(arg) or arg in dir(builtins):
        return arg + '_'
    else:
        return arg


class EnumBase:

    @classmethod
    def _missing_(cls: EnumMeta, key):
        return next((m for m in cls.__members__.values() if m.value.lower() == key.lower()), None)


class BaseMeta(type):

    def __call__(cls, *args, **kwargs):
        normalised_kwargs = {}
        for arg, value in kwargs.items():
            arg = _normalise_arg(arg)

            if not arg.isupper():
                snake_case_arg = inflection.underscore(arg)
                if snake_case_arg != arg:
                    if snake_case_arg in kwargs:
                        raise ValueError('{} and {} both specified'.format(arg, snake_case_arg))

                    normalised_kwargs[snake_case_arg] = value
                else:
                    normalised_kwargs[arg] = value
            else:
                normalised_kwargs[arg] = value

        obj = cls.__new__(cls, *args, **normalised_kwargs)
        obj.__init__(*args, **normalised_kwargs)
        return obj


class Base(metaclass=BaseMeta):

    """The base class for all generated classes"""

    __properties = set()

    def __init__(self):
        self.__calced_hash: int = None

    def __getattr__(self, item):
        snake_case_item = inflection.underscore(item)
        if snake_case_item in super().__getattribute__('properties')():
            return super().__getattribute__(snake_case_item)
        else:
            return super().__getattribute__(item)

    def __setattr__(self, key, value):
        properties = super().__getattribute__('properties')()
        snake_case_key = inflection.underscore(key)
        key_is_property = key in properties
        snake_case_key_is_property = snake_case_key in properties

        if snake_case_key_is_property and not key_is_property:
            return super().__setattr__(snake_case_key, value)
        else:
            return super().__setattr__(key, value)

    def _property_changed(self, prop: str):
        self.__calced_hash = None

    def __hash__(self) -> int:
        if self.__calced_hash is None:
            properties = iter(self.properties())
            prop = next(properties, None)
            calced_hash = hash(super().__getattribute__(prop)) if prop else 1
            for prop in properties:
                calced_hash ^= hash(super().__getattribute__(prop))

            self.__calced_hash = calced_hash

        return self.__calced_hash

    def __eq__(self, other) -> bool:
        return\
            type(self) == type(other) and\
            (self.__calced_hash is None or other.__calced_hash is None or self.__calced_hash == other.__calced_hash) and\
            all(super(Base, self).__getattribute__(p) == super(Base, other).__getattribute__(p) for p in self.properties())

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    def clone(self, **kwargs):
        """
            Clone this object, overriding specified values

            :param kwargs: property names and values, e.g. swap.clone(fixed_rate=0.01)

            **Examples**

            To change the market data location of the default context:

            >>> from gs_quant.instrument import IRCap
            >>> cap = IRCap('5y', 'GBP')
            >>>
            >>> new_cap = cap.clone(cap_rate=0.01)
        """
        clone = copy.copy(self)
        properties = self.properties()
        for key, value in kwargs.items():
            if key in properties:
                setattr(clone, key, value)
            else:
                raise ValueError('Only properties may be passed as kwargs')

        return clone

    @classmethod
    def properties(cls) -> set:
        """The public property names of this class"""
        if not cls.__properties:
            cls.__properties = set(i for i in dir(cls) if isinstance(getattr(cls, i), property) and not i.startswith('_'))
        return cls.__properties

    def as_dict(self, as_camel_case: bool=False) -> dict:
        """Dictionary of the public, non-null properties and values"""
        raw_properties = self.properties()
        properties = (inflection.camelize(p, uppercase_first_letter=False) for p in raw_properties) if as_camel_case else raw_properties
        values = (super(Base, self).__getattribute__(p) for p in raw_properties)
        return dict((p, v) for p, v in zip(properties, values) if v is not None)

    @classmethod
    def prop_type(cls, prop: str) -> type:
        return_hints = get_type_hints(getattr(cls, prop).fget).get('return')
        if hasattr(return_hints, '__origin__'):
            prop_type = return_hints.__origin__
        else:
            prop_type = return_hints

        if prop_type == Union:
            prop_type = next((a for a in return_hints.__args__ if issubclass(a, (Base, EnumBase))), None)

        return prop_type

    @classmethod
    def prop_item_type(cls, prop: str) -> type:
        return_hints = get_type_hints(getattr(cls, prop).fget).get('return')
        return return_hints.__args__[0]

    def __from_dict(self, values: dict):
        for prop in self.properties():
            prop_value = values.get(prop, values.get(inflection.camelize(prop, uppercase_first_letter=False)))

            if prop_value is not None:
                prop_type = self.prop_type(prop)

                if prop_type is None:
                    # This shouldn't happen
                    setattr(self, prop, prop_value)
                elif issubclass(prop_type, dt.datetime):
                    if isinstance(prop_value, int):
                        setattr(self, prop, dt.datetime.fromtimestamp(prop_value / 1000).isoformat())
                    else:
                        import re
                        matcher = re.search('\.([0-9]*)Z$', prop_value)
                        if matcher:
                            sub_seconds = matcher.group(1)
                            if len(sub_seconds) > 6:
                                prop_value = re.sub(matcher.re, '.{}Z'.format(sub_seconds[:6]), prop_value)
                        setattr(self, prop, dateutil.parser.isoparse(prop_value))
                elif issubclass(prop_type, dt.date):
                    setattr(self, prop, dateutil.parser.isoparse(prop_value).date())
                elif issubclass(prop_type, EnumBase):
                    setattr(self, prop, get_enum_value(prop_type, prop_value))
                elif issubclass(prop_type, Base):
                    if isinstance(prop_value, Base):
                        setattr(self, prop, prop_value)
                    else:
                        setattr(self, prop, prop_type.from_dict(prop_value))
                elif issubclass(prop_type, (list, tuple)):
                    item_type = self.prop_item_type(prop)
                    item_args = [i for i in getattr(item_type, '__args__', ()) if isinstance(i, type)]
                    if item_args:
                        item_type = next((a for a in item_args if issubclass(a, (Base, EnumBase))), item_args[-1])

                    if issubclass(item_type, Base):
                        item_values = tuple(v if isinstance(v, (Base, EnumBase)) else item_type.from_dict(v) for v in prop_value)
                    elif issubclass(item_type, EnumBase):
                        item_values = tuple(get_enum_value(item_type, v) for v in prop_value)
                    else:
                        item_values = tuple(prop_value)
                    setattr(self, prop, item_values)
                else:
                    setattr(self, prop, prop_value)

    @classmethod
    def _from_dict(cls, values: dict) -> 'Base':
        args = [k for k, v in signature(cls.__init__).parameters.items() if k != 'kwargs' and v.default == Parameter.empty][1:]
        required = {}

        for arg in args:
            prop_name = arg[:-1] if arg.endswith('_') and not keyword.iskeyword(arg) else arg
            prop_type = cls.prop_type(prop_name)
            value = values.pop(arg, None)

            if prop_type:
                if issubclass(prop_type, Base):
                    if isinstance(value, dict):
                        value = prop_type.from_dict(value)
                elif issubclass(prop_type, EnumBase):
                    value = get_enum_value(prop_type, value)

            required[arg] = value

        instance = cls(**required)
        instance.__from_dict(values)
        return instance

    @classmethod
    def from_dict(cls, values: dict) -> 'Base':
        """
        Construct an instance of this type from a dictionary

        :param values: a dictionary (potentially nested)
        :return: an instance of this type, populated with values
        """
        return cls._from_dict(values)

    @classmethod
    def default_instance(cls) -> 'Base':
        """
        Construct a default instance of this type
        """
        args = [k for k, v in signature(cls.__init__).parameters.items() if v.default == Parameter.empty][1:]
        required = {a: None for a in args}
        return cls(**required)


class Priceable(Base):

    """A priceable, such as a derivative instrument"""

    PROVIDER = None

    def __init__(self):
        super().__init__()
        self._resolution_info: dict = None
        self.unresolved: Priceable = None

    def __getattribute__(self, name):
        resolved = False

        try:
            resolved = super().__getattribute__('_resolution_info') is not None
        except AttributeError:
            pass

        from gs_quant.session import GsSession

        if GsSession.current_is_set and not resolved:
            attr = getattr(super().__getattribute__('__class__'), name, None)
            if attr and isinstance(attr, property) and super().__getattribute__(name) is None:
                self.resolve()

        return super().__getattribute__(name)

    def _property_changed(self, prop: str):
        super()._property_changed(prop)
        self._resolution_info = None

    def get_quantity(self) -> float:
        """
        Quantity of the instrument
        """
        return 1

    def provider(self) -> 'RiskApi':
        """
        The risk provider - defaults to GsRiskApi
        """
        if self.PROVIDER is None:
            from gs_quant.api.gs.risk import GsRiskApi
            self.PROVIDER = GsRiskApi

        return self.PROVIDER

    def resolve(self, in_place: bool=True) -> Optional['Priceable']:
        """
        Resolve non-supplied properties of an instrument

        **Examples**

        >>> from gs_quant.instrument import IRSwap
        >>>
        >>> swap = IRSwap('Pay', '10y', 'USD')
        >>> rate = swap.fixedRate

        rate is None

        >>> swap.resolve()
        >>> rate = swap.fixedRate

        rates is now the solved fixed rate
        """
        from gs_quant.risk import PricingContext
        return PricingContext.current.resolve_fields(self, in_place)

    def dollar_price(self) -> Union[float, Future]:
        """
        Present value in USD

        :return:  a float or a future, depending on whether the current PricingContext is async, or has been entered

        **Examples**

        >>> from gs_quant.instrument import IRCap
        >>>
        >>> cap = IRCap('1y', 'EUR')
        >>> price = cap.dollar_price()

        price is the present value in USD (a float)

        >>> cap_usd = IRCap('1y', 'USD')
        >>> cap_eur = IRCap('1y', 'EUR')
        >>>
        >>> from gs_quant.risk import PricingContext
        >>>
        >>> with PricingContext():
        >>>     price_usd_f = cap_usd.dollar_price()
        >>>     price_eur_f = cap_eur.dollar_price()
        >>>
        >>> price_usd = price_usd_f.result()
        >>> price_eur = price_eur_f.result()

        price_usd_f and price_eur_f are futures, price_usd and price_eur are floats
        """

        from gs_quant.risk import DollarPrice
        return self.calc(DollarPrice)

    def price(self) -> Union[float, Future]:
        """
        Present value in local currency. Note that this is not yet supported on all instruments

        ***Examples**

        >>> from gs_quant.instrument import IRSwap
        >>>
        >>> swap = IRSwap('Pay', '10y', 'EUR')
        >>> price = swap.price()

        price is the present value in EUR (a float)
        """
        from gs_quant.risk import Price
        return self.calc(Price)

    def calc(self, risk_measure: 'RiskMeasure') -> Union[float, pd.DataFrame, Future]:
        """
        Calculate the value of the risk_measure

        :param risk_measure: the risk measure to compute, e.g. IRDelta (from gs_quant.risk)
        :return: a float or dataframe, depending on whether the value is scalar or structured, or a future thereof (depending on how PricingContext is being used)

        **Examples**

        >>> from gs_quant.instrument import IRCap
        >>> from gs_quant.risk import IRDelta
        >>>
        >>> cap = IRCap('1y', 'USD')
        >>> delta = cap.calc(IRDelta)

        delta is a dataframe

        >>> from gs_quant.instrument import EqOption
        >>> from gs_quant.risk import EqDelta
        >>>
        >>> option = EqOption('.SPX', '3m', 'ATMF', 'Call', 'European')
        >>> delta = option.calc(EqDelta)

        delta is a float

        >>> from gs_quant.risk import PricingContext
        >>>
        >>> cap_usd = IRCap('1y', 'USD')
        >>> cap_eur = IRCap('1y', 'EUR')

        >>> with PricingContext():
        >>>     usd_delta_f = cap_usd.calc(IRDelta)
        >>>     eur_delta_f = cap_eur.calc(IRDelta)
        >>>
        >>> usd_delta = usd_delta_f.result()
        >>> eur_delta = eur_delta_f.result()

        usd_delta_f and eur_delta_f are futures, usd_delta and eur_delta are dataframes
        """
        from gs_quant.risk import PricingContext, get_specific_risk_measure
        specific_risk_measure = get_specific_risk_measure(risk_measure, self)
        if specific_risk_measure is None:
            raise ValueError('Unsupported risk measure')

        return PricingContext.current.calc(self, specific_risk_measure)


class Instrument(Priceable):

    __asset_class_and_type_to_instrument = {}

    @classmethod
    def asset_class_and_type_to_instrument(cls) -> Mapping[Tuple['AssetClass', 'AssetType'], 'Instrument']:
        if not cls.__asset_class_and_type_to_instrument:
            import gs_quant.target.instrument as instrument
            import inspect
            instrument_classes = [c for _, c in inspect.getmembers(instrument, inspect.isclass) if
                                  issubclass(c, Instrument) and c is not Instrument]

            cls.__asset_class_and_type_to_instrument[(instrument.AssetClass.Cash, instrument.AssetType.Currency)] = instrument.Forward

            for clazz in instrument_classes:
                instrument = clazz.default_instance()
                cls.__asset_class_and_type_to_instrument[(instrument.asset_class, instrument.type)] = clazz

        return cls.__asset_class_and_type_to_instrument

    @classmethod
    def from_dict(cls, values: dict) -> Optional[Union['Instrument', 'Security']]:
        from gs_quant.target.instrument import AssetClass, AssetType

        if not values:
            return None

        instrument_type = cls.asset_class_and_type_to_instrument().get((
            get_enum_value(AssetClass, values.pop('asset_class')),
            get_enum_value(AssetType, values.pop('type'))))

        if instrument_type:
            return instrument_type._from_dict(values)
        else:
            from gs_quant.instrument import Security
            return Security.from_dict(values)


def get_enum_value(enum_type: EnumMeta, value: Union[EnumBase, str]):
    if value in (None, 'None'):
        return None

    if isinstance(value, enum_type):
        return value

    try:
        enum_value = enum_type(value)
    except ValueError:
        _logger.warning('Setting value to {}, which is not a valid entry in {}'.format(value, enum_type))
        enum_value = value

    return enum_value
