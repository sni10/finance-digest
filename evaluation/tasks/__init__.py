from .classify import ClassifyTask
from .extract import ExtractTask
from .same_event import SameEventTask
from .verify import VerifyTask

TASKS = {task.name: task for task in (ClassifyTask, SameEventTask, ExtractTask, VerifyTask)}
