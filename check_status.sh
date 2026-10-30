#!/bin/bash

# Backend Status Checker

echo "========================================"
echo "🔍 Flask Backend Status Check"
echo "========================================"

# Check if backend is running
if lsof -ti:8587 > /dev/null 2>&1; then
    echo "✅ Backend is RUNNING"
    echo "   Process IDs: $(lsof -ti:8587 | tr '\n' ' ')"
else
    echo "❌ Backend is NOT running"
    echo ""
    echo "To start it, run:"
    echo "  bash run_backend.sh"
    exit 1
fi

# Test API endpoint
echo ""
echo "🧪 Testing API endpoint..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8587/api/post/all)

if [ "$HTTP_CODE" == "200" ]; then
    echo "✅ API is responding (HTTP $HTTP_CODE)"
    echo ""
    echo "📊 Sample response:"
    curl -s http://localhost:8587/api/post/all | head -100
elif [ "$HTTP_CODE" == "401" ]; then
    echo "❌ Still getting 401 error"
    echo "   The backend needs to be restarted with updated code"
else
    echo "⚠️  API returned HTTP $HTTP_CODE"
fi

echo ""
echo "========================================"
echo "📝 Recent Backend Activity:"
echo "========================================"
tail -5 backend.log 2>/dev/null || echo "No logs found"

echo ""
echo "========================================"
echo "🌐 Access Points:"
echo "========================================"
echo "   API: http://localhost:8587/api/post/all"
echo "   Logs: tail -f ~/flaskbackend/backend.log"
echo ""
echo "To stop backend: pkill -f 'python main.py'"
echo "To start backend: bash run_backend.sh"
echo "========================================"

