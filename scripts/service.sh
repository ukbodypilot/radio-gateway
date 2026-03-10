#!/bin/bash
# Radio Gateway — systemd service manager
# Usage: ./scripts/service.sh [install|uninstall|start|stop|restart|status|logs|enable|disable]

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SERVICE_FILE="$SCRIPT_DIR/radio-gateway.service"
SERVICE_NAME="radio-gateway"

case "$1" in
    install)
        echo "Installing $SERVICE_NAME service..."
        # Update paths in service file to match current location
        GATEWAY_DIR="$(dirname "$SCRIPT_DIR")"
        sed "s|WorkingDirectory=.*|WorkingDirectory=$GATEWAY_DIR|; \
             s|ExecStart=.*|ExecStart=$GATEWAY_DIR/start.sh|; \
             s|User=.*|User=$(whoami)|; \
             s|Group=.*|Group=$(id -gn)|; \
             s|Environment=HOME=.*|Environment=HOME=$HOME|; \
             s|/run/user/1000|/run/user/$(id -u)|g" \
            "$SERVICE_FILE" | sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null
        sudo systemctl daemon-reload
        # Enable lingering so PipeWire/user session starts at boot without login
        sudo loginctl enable-linger "$(whoami)"
        echo "Installed. Commands:"
        echo "  ./scripts/service.sh enable   — start on boot"
        echo "  ./scripts/service.sh start    — start now"
        echo "  ./scripts/service.sh logs     — view logs"
        echo ""
        echo "IMPORTANT: Set HEADLESS_MODE = true in gateway_config.txt"
        echo "  for proper headless operation (no console status bar)"
        ;;
    uninstall)
        echo "Uninstalling $SERVICE_NAME service..."
        sudo systemctl stop $SERVICE_NAME 2>/dev/null
        sudo systemctl disable $SERVICE_NAME 2>/dev/null
        sudo rm -f /etc/systemd/system/$SERVICE_NAME.service
        sudo systemctl daemon-reload
        echo "Done."
        ;;
    start)
        sudo systemctl start $SERVICE_NAME
        sleep 1
        systemctl status $SERVICE_NAME --no-pager
        ;;
    stop)
        sudo systemctl stop $SERVICE_NAME
        echo "Stopped."
        ;;
    restart)
        sudo systemctl restart $SERVICE_NAME
        sleep 1
        systemctl status $SERVICE_NAME --no-pager
        ;;
    status)
        systemctl status $SERVICE_NAME --no-pager
        ;;
    logs)
        # Follow live logs from journalctl
        journalctl -u $SERVICE_NAME -f --no-hostname -o cat
        ;;
    enable)
        sudo systemctl enable $SERVICE_NAME
        echo "Will start on boot."
        ;;
    disable)
        sudo systemctl disable $SERVICE_NAME
        echo "Will NOT start on boot."
        ;;
    *)
        echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs|enable|disable}"
        echo ""
        echo "Quick start:"
        echo "  $0 install    — install systemd service"
        echo "  $0 enable     — start on boot"
        echo "  $0 start      — start now"
        echo "  $0 logs       — follow live logs (journalctl)"
        echo "  $0 stop       — stop the gateway"
        echo ""
        echo "For interactive (console) mode, just run: ./start.sh"
        exit 1
        ;;
esac
