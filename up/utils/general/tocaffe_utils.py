import torch
import numpy as np
from collections.abc import Iterable, Mapping
from up.utils.general.log_helper import default_logger as logger


def detach(x):
    """detach from given tensor to block the trace route"""
    if torch.is_tensor(x):
        shape = tuple(map(int, x.shape))
        return torch.zeros(shape, dtype=x.dtype, device=x.device)
    elif isinstance(x, str) or isinstance(x, bytes):  # there is a dead loop when x is a str with len(x)=1n
        return x
    elif isinstance(x, np.ndarray):  # numpy recommends building array by calling np.array
        return np.array(list(map(detach, x)))
    elif isinstance(x, Mapping):
        return type(x)((k, detach(v)) for k, v in x.items())
    elif isinstance(x, Iterable):
        try:
            output = type(x)(map(detach, x))
        except Exception as e:
            logger.info(x)
            raise e
        return output

    else:
        return x


def get_model_hash(model_state_dict):
    try:
        from spring_analytics.model_lineage import get_model_meta, update_model_kestrel
        model_meta = get_model_meta(model_state_dict)
        update_model_kestrel(model_meta)
        model_hash, model_short_hash, model_size_bytes = model_meta
    except Exception:
        return None
    return model_hash


def rewrite_onnx(onnx_prefix, model_hash):
    import onnx
    onnx_file = onnx_prefix + '.onnx'
    onnx_model = onnx.load(onnx_file)
    onnx_model.doc_string += f'''
        model_hash: {model_hash}
    '''
    onnx.save(onnx_model, onnx_file)


def rewrite_caffe(caffe_prefix, model_hash):
    from spring.nart.tools.proto import caffe_pb2
    caffe_file = caffe_prefix + '.caffemodel'
    caffe_model = caffe_pb2.NetParameter()
    with open(caffe_file, "rb") as f:
        caffe_model.ParseFromString(f.read())
    caffe_model.name += f'\n# model_hash: {model_hash}'
    with open(caffe_file, 'wb') as f:
        f.write(caffe_model.SerializeToString())


def rewrite_model(prefix, model_hash):
    if model_hash is None:
        return
    try:
        rewrite_onnx(prefix, model_hash)
        rewrite_caffe(prefix, model_hash)
    except Exception:
        return


class ToCaffe(object):
    _tocaffe = False

    @classmethod
    def disable_trace(self, func):
        def wrapper(*args, **kwargs):
            output = func(*args, **kwargs)
            if not self._tocaffe:
                return output
            else:
                return detach(output)
        return wrapper

    @classmethod
    def prepare(self):
        if self._tocaffe:
            return

        self._tocaffe = True

        # workaround to avoid onnx tracing tensor.shape
        torch.Tensor._shpae = torch.Tensor.shape

        @property
        def shape(self):
            return self.detach()._shpae
        torch.Tensor.shape = shape
