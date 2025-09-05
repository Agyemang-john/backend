from channels.generic.websocket import AsyncWebsocketConsumer
import json
import logging

# Set up logger
logger = logging.getLogger(__name__)

class VendorOrderConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.seller_id = self.scope['url_route']['kwargs']['seller_id']
        self.group_name = f"seller_{self.seller_id}"

        logger.info(f"Seller {self.seller_id} is attempting to connect to group {self.group_name}")

        try:
            # Join seller group
            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name
            )
            await self.accept()
            logger.info(f"Seller {self.seller_id} successfully connected to group {self.group_name}")
        except Exception as e:
            logger.error(f"Error connecting seller {self.seller_id} to group {self.group_name}: {e}")
            await self.close()

    async def disconnect(self, close_code):
        logger.info(f"Seller {self.seller_id} is disconnecting from group {self.group_name} with close code {close_code}")

        try:
            if self.group_name:
                await self.channel_layer.group_discard(self.group_name, self.channel_name)
                logger.info(f"Seller {self.seller_id} successfully removed from group {self.group_name}")
        except Exception as e:
            logger.error(f"Error removing seller {self.seller_id} from group {self.group_name}: {e}")

    async def new_order_notification(self, event):
        try:
            logger.info(f"Sending new order notification to seller {self.seller_id}: {event['message']}")
            await self.send(text_data=json.dumps(event["message"]))
            logger.info(f"Notification sent to seller {self.seller_id}")
        except Exception as e:
            logger.error(f"Error sending notification to seller {self.seller_id}: {e}")
