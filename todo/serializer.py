from services.serialization import BaseSerializer, SerializerField
from todo.model import ToDo


class ToDoSerializer(BaseSerializer):
    model = ToDo
    fields = [
        SerializerField("id", "primary_key"),
        SerializerField("comment", "instruction"),
        SerializerField("crated_at", "creation_time"),
        SerializerField("priority", "preference"),
        SerializerField("is main", "is_principal)"),
        SerializerField("worker_fullname", "worker"),
        SerializerField("due_date", "deadline"),
        SerializerField("count", "amount"),
    ]
