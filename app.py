"""Lymow app entry point."""

from homey.app import App


class LymowApp(App):
    async def on_init(self) -> None:
        self.log("Lymow app has been initialized")


homey_export = LymowApp
