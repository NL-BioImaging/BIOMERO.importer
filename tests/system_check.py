#!/usr/bin/env python3
import yaml
import json
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from biomero_importer.utils.ingest_tracker import Base, IngestionTracking, Preprocessing, STAGE_NEW_ORDER, _mask_url
from pathlib import Path
import datetime
import shutil

def load_config(path="/auto-importer/config/settings.yml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)
    
# Function to copy sample image to the target directory
def copy_sample_image(core_group_name, dataset, sample_image, base_dir):
    # Shortcut: If sample_image is already within base_dir, just return the path
    if sample_image.is_relative_to(base_dir / core_group_name):
        print(f"Sample image is already in base directory, returning path: {sample_image}")
        return sample_image
    
    target_dir = base_dir / core_group_name / ".omerodata2" / dataset.replace('_', '/')
    target_dir.mkdir(parents=True, exist_ok=True) 
    # Determine target path
    if sample_image.suffix == '.zarr':  # Check if it's a Zarr directory
        target_path = target_dir / f"sample_image_{core_group_name}.zarr"
        shutil.copytree(sample_image, target_path)
        print(f"Copied Zarr directory to '{target_path}'")
    else:
        target_path = target_dir / f"sample_image_{core_group_name}.tif"
        shutil.copy(sample_image, target_path)
        print(f"Copied sample image to '{target_path}'")
    return target_path

def main():
    # Load configuration and get the database URL
    config = load_config()
    db_url = config["ingest_tracking_db"]
    print(f"Using database URL: {_mask_url(db_url)}")
    
    # Create the engine and sessionmaker
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    
    # Create tables if they do not exist
    Base.metadata.create_all(engine)
    
    session = Session()
    try:
        files = ["/auto-importer/tests/Barbie1.tif"]
        
        # Sample image and base directory
        sample_image = Path(config.get('sample_image', '/auto-importer/tests/Barbie1.tif'))
        sample_group = config.get('sample_group', 'Reits')
        sample_user = config.get('sample_user', 'rrosas')
        sample_parent_id = config.get('sample_parent_id', '2401')
        sample_parent_type = config.get('sample_parent_type', 'Dataset')
        base_dir = Path(config['base_dir'])
        
        # Copy the sample image
        file = copy_sample_image("coreReits", datetime.datetime.now().strftime("%Y_%m_%d_%H-%M-%S"), sample_image, base_dir)
        files = [str(file)]
            
        # Build a sample upload order record
        order_info = {
            "Group": sample_group,
            "Username": sample_user,
            "DestinationID": sample_parent_id,  # Updated from "DataPackage" to "DestinationID"
            "DestinationType": sample_parent_type, # Screen
            "UUID": str(uuid.uuid4()),
            "Files": files
        }
        
        preprocessing = config.get('preprocessing')
        if preprocessing: #exists
            
            sample_pre_container = config.get('sample_pre_container', "cellularimagingcf/cimagexpresstoometiff:v0.7")
            sample_pre_input = config.get('sample_pre_input', "{Files}")
            sample_pre_out = config.get('sample_pre_out', "/data")
            sample_pre_outalt = config.get('sample_pre_outalt', "/out")
            sample_pre_save = config.get('sample_pre_save', "single")
            
            order_info["preprocessing_container"] = sample_pre_container
            order_info["preprocessing_inputfile"] = sample_pre_input
            order_info["preprocessing_outputfolder"] = sample_pre_out # local to the container / a mount point
            order_info["preprocessing_altoutputfolder"] = sample_pre_outalt # local to the container / a mount point
            order_info["extra_params"] = {
                "saveoption": sample_pre_save
            }
        
        # Create a new IngestionTracking instance
        new_order = IngestionTracking(
            group_name=order_info["Group"],
            user_name=order_info["Username"],
            destination_id=order_info["DestinationID"],
            destination_type=order_info["DestinationType"],
            stage=STAGE_NEW_ORDER,
            uuid=order_info["UUID"],
            files=order_info["Files"]
        )
        
        session.add(new_order)
        
        # Add preprocessing data if needed
        preprocessing = config.get('preprocessing')
        if preprocessing:
            new_order.preprocessing = Preprocessing(
                container=order_info["preprocessing_container"],
                input_file=order_info["preprocessing_inputfile"],
                output_folder=order_info["preprocessing_outputfolder"],
                alt_output_folder= order_info["preprocessing_altoutputfolder"],
                extra_params=order_info.get("extra_params")  # Store the extra parameters
            )

        # Commit everything in ONE transaction
        session.commit()
        
        print(f"New upload order created with ID: {new_order.id} -> {new_order.__dict__}")
        if preprocessing:
            print(f"Preprocessing entry created for order ID: {new_order.id} -> {new_order.preprocessing.__dict__}")
    except Exception as e:
        print("Error creating upload order:", e)
        session.rollback()
    finally:
        session.close()

if __name__ == "__main__":
    main()
