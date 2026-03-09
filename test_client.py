"""
Example Test Client for MCP Assistant Service

This demonstrates how to interact with the /assist endpoint
for both regular messages and elicitation responses.
"""

import httpx
import asyncio
import json
from datetime import datetime

BASE_URL = "http://localhost:8000"

async def test_simple_message():
    """Test sending a simple message (no elicitation)"""
    
    print("\n" + "="*80)
    print("TEST 1: Simple Message (No Elicitation)")
    print("="*80)
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/assist",
            json={
                "request_type": "message",
                "message": "Hello, how are you?",
                "thread_id": "test-simple-123",
                "user_id": "test-user",
                "organization_id": "test-org",
                "auth_token": "test-token"
            }
        )
        
        data = response.json()
        print(f"\nStatus: {data['status']}")
        print(f"Response: {data['response']}")
        print(f"Pending Elicitation: {data.get('pending_elicitation')}")

async def test_message_with_elicitation():
    """Test message that triggers elicitation"""
    
    print("\n" + "="*80)
    print("TEST 2: Message with Elicitation")
    print("="*80)
    
    thread_id = f"test-elic-{datetime.now().timestamp()}"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Send initial message
        print("\n[Step 1] Sending message that requires tool execution...")
        
        response1 = await client.post(
            f"{BASE_URL}/assist",
            json={
                "request_type": "message",
                "message": "Use tool fetch_sales_data",  # This will trigger tool call
                "thread_id": thread_id,
                "user_id": "test-user",
                "organization_id": "test-org",
                "auth_token": "test-token"
            }
        )
        
        data1 = response1.json()
        print(f"Status: {data1['status']}")
        print(f"Response: {data1['response']}")
        
        if data1['status'] == 'awaiting_elicitation':
            elicitation = data1['pending_elicitation']
            print(f"\n[Elicitation Required]")
            print(f"  ID: {elicitation['elicitation_id']}")
            print(f"  Message: {elicitation['message']}")
            print(f"  Schema: {json.dumps(elicitation['schema'], indent=2)}")
            
            # Step 2: Wait a bit (simulate user filling form)
            print("\n[Step 2] Simulating user filling form (waiting 2 seconds)...")
            await asyncio.sleep(2)
            
            # Step 3: Submit elicitation response
            print("\n[Step 3] Submitting elicitation response...")
            
            response2 = await client.post(
                f"{BASE_URL}/assist",
                json={
                    "request_type": "elicitation_response",
                    "thread_id": thread_id,
                    "user_id": "test-user",
                    "organization_id": "test-org",
                    "auth_token": "test-token",
                    "elicitation_id": elicitation['elicitation_id'],
                    "elicitation_data": {
                        "date_range": "2024-01-01 to 2024-01-31",
                        "region": "North America"
                    }
                }
            )
            
            data2 = response2.json()
            print(f"\nFinal Status: {data2['status']}")
            print(f"Final Response: {data2['response']}")
        else:
            print("\nNo elicitation required (unexpected)")

async def test_session_status():
    """Test checking session status"""
    
    print("\n" + "="*80)
    print("TEST 3: Check Session Status")
    print("="*80)
    
    thread_id = "test-status-123"
    
    async with httpx.AsyncClient() as client:
        # First create an elicitation
        response1 = await client.post(
            f"{BASE_URL}/assist",
            json={
                "request_type": "message",
                "message": "Use tool fetch_sales_data",
                "thread_id": thread_id,
                "user_id": "test-user",
                "organization_id": "test-org",
                "auth_token": "test-token"
            }
        )
        
        # Then check status
        response2 = await client.get(f"{BASE_URL}/session/{thread_id}/status")
        
        status_data = response2.json()
        print(f"\nThread ID: {status_data['thread_id']}")
        print(f"Has Pending: {status_data['has_pending']}")
        print(f"Pending Elicitations: {json.dumps(status_data['pending_elicitations'], indent=2)}")

async def test_health_check():
    """Test health check endpoint"""
    
    print("\n" + "="*80)
    print("TEST 4: Health Check")
    print("="*80)
    
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/health")
        
        data = response.json()
        print(f"\nHealth Status: {data['status']}")
        print(f"Service: {data['service']}")
        print(f"Timestamp: {data['timestamp']}")

async def test_elicitation_timeout():
    """Test elicitation timeout behavior"""
    
    print("\n" + "="*80)
    print("TEST 5: Elicitation Timeout (Warning: Takes 3+ minutes)")
    print("="*80)
    
    print("\nThis test will:")
    print("1. Trigger an elicitation")
    print("2. Wait for timeout (180 seconds)")
    print("3. Verify timeout handling")
    print("\nSkipping for now (uncomment to run)")
    
    # Uncomment to actually run timeout test
    # thread_id = f"test-timeout-{datetime.now().timestamp()}"
    # 
    # async with httpx.AsyncClient(timeout=200.0) as client:
    #     # Trigger elicitation
    #     response1 = await client.post(...)
    #     
    #     # Wait for timeout
    #     print("Waiting for timeout (180 seconds)...")
    #     await asyncio.sleep(185)
    #     
    #     # Check status
    #     response2 = await client.get(f"{BASE_URL}/session/{thread_id}/status")
    #     # Should show timeout status

async def run_all_tests():
    """Run all tests in sequence"""
    
    print("\n" + "="*80)
    print("MCP ASSISTANT SERVICE - TEST SUITE")
    print("="*80)
    print(f"Target: {BASE_URL}")
    print(f"Started: {datetime.now().isoformat()}")
    
    try:
        await test_health_check()
        await test_simple_message()
        await test_message_with_elicitation()
        await test_session_status()
        # await test_elicitation_timeout()  # Uncomment to test timeout
        
        print("\n" + "="*80)
        print("ALL TESTS COMPLETED")
        print("="*80)
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Run tests
    asyncio.run(run_all_tests())
