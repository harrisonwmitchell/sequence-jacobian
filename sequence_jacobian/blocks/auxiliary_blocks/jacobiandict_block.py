"""A simple wrapper for JacobianDicts to be embedded in DAGs"""

from numbers import Real
from typing import Dict, Union, List

from ...primitives import Block, Array
from ...jacobian.classes import JacobianDict


class JacobianDictBlock(JacobianDict, Block):
    """A wrapper for nested dicts/JacobianDicts passed directly into DAGs to ensure method compatibility"""
    def __init__(self, nesteddict, outputs=None, inputs=None, name=None):
        super().__init__(nesteddict, outputs=outputs, inputs=inputs, name=name)

    def __repr__(self):
        return f"<JacobianDictBlock outputs={self.outputs}, inputs={self.inputs}>"

    def impulse_linear(self, ss: Dict[str, Union[Real, Array]],
                       exogenous: Dict[str, Array], **kwargs) -> Dict[str, Array]:
        return self.jacobian(list(exogenous.keys())).apply(exogenous)

    def jacobian(self, exogenous: List[str] = None, **kwargs) -> JacobianDict:
        if exogenous is None:
            return JacobianDict(self.nesteddict)
        else:
            return self[:, exogenous]
